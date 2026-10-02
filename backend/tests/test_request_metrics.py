"""End-to-end checks that inference calls are billed into the metrics registry.

These assert the wiring (middleware → registry, endpoint → registry) rather than
the registry's arithmetic, which ``tests/test_metrics.py`` covers directly.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import httpx
import pytest
from app.core.metrics import metrics

from tests.conftest import build_mock_client

COMPLETION = {
    "id": "chatcmpl-abc",
    "model": "nvidia/Nemotron-3_5-Lightning",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}}],
    "usage": {"prompt_tokens": 9, "completion_tokens": 1, "total_tokens": 10},
}

STREAM_FRAMES = (
    'data: {"choices":[{"delta":{"content":"He"}}]}\n\n'
    'data: {"choices":[{"delta":{"content":"llo"}}],'
    '"usage":{"prompt_tokens":7,"completion_tokens":2,"total_tokens":9}}\n\n'
    "data: [DONE]\n\n"
)


@pytest.fixture(autouse=True)
def _clean_metrics() -> Iterator[None]:
    """Isolate the process-global registry between tests."""
    metrics.reset()
    yield
    metrics.reset()


# ---------------------------------------------------------------------------
# Request lifecycle
# ---------------------------------------------------------------------------
async def test_request_is_recorded_against_its_route(client: httpx.AsyncClient) -> None:
    await client.get("/api/v1/models/catalog")

    snapshot = metrics.snapshot()
    assert snapshot["requests"]["total"] >= 1
    # The label is the matched route template, not the raw request path.
    assert "/models/catalog" in snapshot["routes"]
    assert snapshot["requests"]["in_flight"] == 0


async def test_every_response_carries_a_request_id(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/models/catalog")

    assert response.headers["X-Request-ID"]
    assert float(response.headers["X-Process-Time-Ms"]) >= 0


async def test_supplied_request_id_is_echoed(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/models/catalog", headers={"X-Request-ID": "trace-me"})

    assert response.headers["X-Request-ID"] == "trace-me"


async def test_server_error_is_counted_as_a_failure(
    client: httpx.AsyncClient, override_nebius
) -> None:
    nebius, http = build_mock_client(lambda _r: httpx.Response(500, json={"error": "boom"}))
    override_nebius(nebius)
    try:
        await client.post(
            "/api/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
    finally:
        await http.aclose()

    assert metrics.snapshot()["requests"]["failed"] >= 1


# ---------------------------------------------------------------------------
# Token consumption
# ---------------------------------------------------------------------------
async def test_completion_usage_reaches_the_registry(
    client: httpx.AsyncClient, override_nebius
) -> None:
    nebius, http = build_mock_client(lambda _r: httpx.Response(200, json=COMPLETION))
    override_nebius(nebius)
    try:
        response = await client.post(
            "/api/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "2+2?"}]},
        )
    finally:
        await http.aclose()

    assert response.status_code == 200
    assert metrics.tokens.prompt_tokens == 9
    assert metrics.tokens.completion_tokens == 1
    assert metrics.tokens.total_tokens == 10
    by_model = metrics.snapshot()["tokens_by_model"]
    assert by_model["nvidia/Nemotron-3_5-Lightning"]["calls"] == 1


async def test_usage_is_bucketed_under_the_resolved_model(
    client: httpx.AsyncClient, override_nebius
) -> None:
    nebius, http = build_mock_client(lambda _r: httpx.Response(200, json=COMPLETION))
    override_nebius(nebius)
    try:
        await client.post(
            "/api/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": "hi"}],
                "model": "nvidia/Nemotron-3_5-Lightning",
            },
        )
    finally:
        await http.aclose()

    assert "nvidia/Nemotron-3_5-Lightning" in metrics.snapshot()["tokens_by_model"]


async def test_streamed_usage_frame_is_billed(client: httpx.AsyncClient, override_nebius) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text=STREAM_FRAMES, headers={"content-type": "text/event-stream"}
        )

    nebius, http = build_mock_client(handler)
    override_nebius(nebius)
    try:
        async with client.stream(
            "POST",
            "/api/v1/chat/completions/stream",
            json={"messages": [{"role": "user", "content": "say hi"}]},
        ) as response:
            assert response.status_code == 200
            payload = [line async for line in response.aiter_lines() if line]
    finally:
        await http.aclose()

    # The frames must reach the browser untouched...
    assert any("[DONE]" in line for line in payload)
    # ...while the terminal usage frame is still accounted for. The frames carry
    # no model id, so usage is attributed to the configured default model.
    assert metrics.tokens.total_tokens == 9
    assert metrics.tokens.prompt_tokens == 7
    assert metrics.tokens.completion_tokens == 2
    by_model = metrics.snapshot()["tokens_by_model"]
    assert by_model["nvidia/nemotron-3-super-120b-a12b"]["calls"] == 1


async def test_stream_requests_usage_in_the_upstream_body(
    client: httpx.AsyncClient, override_nebius
) -> None:
    """The stream must opt into usage reporting, or no usage frame ever arrives."""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, text="data: [DONE]\n\n")

    nebius, http = build_mock_client(handler)
    override_nebius(nebius)
    try:
        async with client.stream(
            "POST",
            "/api/v1/chat/completions/stream",
            json={"messages": [{"role": "user", "content": "hi"}]},
        ) as response:
            await response.aread()
    finally:
        await http.aclose()

    assert seen["stream"] is True
    assert seen["stream_options"] == {"include_usage": True}


async def test_malformed_stream_frames_do_not_break_the_stream(
    client: httpx.AsyncClient, override_nebius
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        garbage = [
            "data: {not json at all",
            'data: {"usage":{"prompt_tokens":"oops"}}',
            "data: [DONE]",
        ]
        return httpx.Response(
            200,
            text="".join(f"{line}\n\n" for line in garbage),
            headers={"content-type": "text/event-stream"},
        )

    nebius, http = build_mock_client(handler)
    override_nebius(nebius)
    try:
        async with client.stream(
            "POST",
            "/api/v1/chat/completions/stream",
            json={"messages": [{"role": "user", "content": "hi"}]},
        ) as response:
            payload = [line async for line in response.aiter_lines() if line]
    finally:
        await http.aclose()

    assert any("[DONE]" in line for line in payload)
    assert metrics.tokens.total_tokens == 0


async def test_failed_completion_does_not_inflate_token_totals(
    client: httpx.AsyncClient, override_nebius
) -> None:
    nebius, http = build_mock_client(lambda _r: httpx.Response(429, json={"error": "slow down"}))
    override_nebius(nebius)
    try:
        await client.post(
            "/api/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
    finally:
        await http.aclose()

    assert metrics.tokens.total_tokens == 0
