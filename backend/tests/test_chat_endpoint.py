"""Tests for the chat completion endpoints."""

from __future__ import annotations

import json

import httpx

from tests.conftest import build_mock_client

UPSTREAM_COMPLETION = {
    "id": "chatcmpl-abc",
    "object": "chat.completion",
    "created": 1_700_000_000,
    "model": "nvidia/Nemotron-3_5-Lightning",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "4"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 9, "completion_tokens": 1, "total_tokens": 10},
}


async def test_completion_is_normalised(client: httpx.AsyncClient, override_nebius) -> None:
    nebius, http = build_mock_client(lambda _r: httpx.Response(200, json=UPSTREAM_COMPLETION))
    override_nebius(nebius)
    try:
        response = await client.post(
            "/api/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": "2+2?"}],
                "model": "nvidia/Nemotron-3_5-Lightning",
                "max_tokens": 32,
            },
        )
    finally:
        await http.aclose()

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "chatcmpl-abc"
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"] == {"role": "assistant", "content": "4"}
    assert body["usage"] == {
        "prompt_tokens": 9,
        "completion_tokens": 1,
        "total_tokens": 10,
    }
    assert body["provider"] == "nebius-token-factory"
    assert body["latency_ms"] is not None


async def test_empty_message_list_is_rejected(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/v1/chat/completions", json={"messages": []})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ValidationError"


async def test_unknown_field_is_rejected(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}], "nope": True},
    )

    assert response.status_code == 422


async def test_stream_endpoint_forwards_sse_frames(
    client: httpx.AsyncClient, override_nebius
) -> None:
    sse = (
        'data: {"choices":[{"delta":{"content":"He"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"llo"}}]}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    nebius, http = build_mock_client(handler)
    override_nebius(nebius)
    try:
        response = await client.post(
            "/api/v1/chat/completions/stream",
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
    finally:
        await http.aclose()

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = [line for line in response.text.split("\n\n") if line.strip()]
    assert frames[0].startswith("data: ")
    assert json.loads(frames[0][len("data: ") :])["choices"][0]["delta"]["content"] == "He"
    assert frames[-1] == "data: [DONE]"


async def test_auth_failure_surfaces_as_401(client: httpx.AsyncClient, override_nebius) -> None:
    nebius, http = build_mock_client(lambda _r: httpx.Response(403, json={"error": "forbidden"}))
    override_nebius(nebius)
    try:
        response = await client.post(
            "/api/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
    finally:
        await http.aclose()

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "NebiusAuthError"


async def test_embeddings_endpoint(client: httpx.AsyncClient, override_nebius) -> None:
    payload = {
        "data": [
            {"index": 0, "embedding": [0.1]},
            {"index": 1, "embedding": [0.2]},
        ]
    }
    nebius, http = build_mock_client(lambda _r: httpx.Response(200, json=payload))
    override_nebius(nebius)
    try:
        response = await client.post("/api/v1/chat/embeddings", json={"input": ["first", "second"]})
    finally:
        await http.aclose()

    assert response.status_code == 200
    assert response.json()["embeddings"] == [[0.1], [0.2]]
