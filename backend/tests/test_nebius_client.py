"""Tests for the Nebius Token Factory client."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from app.core.config import Settings
from app.services.nebius import (
    NebiusAuthError,
    NebiusNotFoundError,
    NebiusRateLimitError,
    NebiusUpstreamError,
)

from tests.conftest import build_mock_client

CHAT_BODY = {
    "id": "chatcmpl-test",
    "object": "chat.completion",
    "created": 1_700_000_000,
    "model": "nvidia/Nemotron-3_5-Lightning",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Hello from Nemotron."},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16},
}


async def test_chat_completion_sends_bearer_token_and_model() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=CHAT_BODY)

    nebius, http = build_mock_client(handler)
    try:
        result = await nebius.create_chat_completion(
            messages=[{"role": "user", "content": "hi"}],
            model="nvidia/Nemotron-3_5-Lightning",
        )
    finally:
        await http.aclose()

    assert captured["auth"] == "Bearer test-nebius-key"
    assert captured["url"].endswith("/v1/chat/completions")
    assert captured["body"]["model"] == "nvidia/Nemotron-3_5-Lightning"
    assert result["choices"][0]["message"]["content"] == "Hello from Nemotron."


async def test_model_allowlist_blocks_unknown_models() -> None:
    config = Settings(MODEL_ALLOWLIST="nvidia/Nemotron-3_5-Lightning")
    nebius, http = build_mock_client(lambda _r: httpx.Response(200, json=CHAT_BODY), config=config)
    try:
        with pytest.raises(NebiusNotFoundError, match="not in MODEL_ALLOWLIST"):
            await nebius.create_chat_completion(
                messages=[{"role": "user", "content": "hi"}],
                model="evil/model",
            )
    finally:
        await http.aclose()


async def test_auth_error_is_mapped() -> None:
    nebius, http = build_mock_client(
        lambda _r: httpx.Response(401, json={"error": "invalid api key"})
    )
    try:
        with pytest.raises(NebiusAuthError, match="rejected the API key"):
            await nebius.create_chat_completion(messages=[{"role": "user", "content": "hi"}])
    finally:
        await http.aclose()


async def test_rate_limit_is_retried_then_succeeds() -> None:
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"error": "slow down"})
        return httpx.Response(200, json=CHAT_BODY)

    nebius, http = build_mock_client(handler)
    try:
        result = await nebius.create_chat_completion(messages=[{"role": "user", "content": "hi"}])
    finally:
        await http.aclose()

    assert attempts == 2
    assert result["id"] == "chatcmpl-test"


async def test_rate_limit_exhausts_retries() -> None:
    nebius, http = build_mock_client(
        lambda _r: httpx.Response(429, headers={"Retry-After": "0"}, json={"error": "nope"})
    )
    try:
        with pytest.raises(NebiusRateLimitError):
            await nebius.create_chat_completion(messages=[{"role": "user", "content": "hi"}])
    finally:
        await http.aclose()


async def test_server_error_is_not_retried_beyond_budget() -> None:
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503, json={"error": "unavailable"})

    nebius, http = build_mock_client(handler)
    try:
        with pytest.raises(NebiusUpstreamError):
            await nebius.create_chat_completion(messages=[{"role": "user", "content": "hi"}])
    finally:
        await http.aclose()

    # NEBIUS_MAX_RETRIES=1 => 1 initial attempt + 1 retry.
    assert attempts == 2


async def test_list_models_returns_entries() -> None:
    payload = {"object": "list", "data": [{"id": "nvidia/Nemotron-3_5-Lightning"}]}
    nebius, http = build_mock_client(lambda _r: httpx.Response(200, json=payload))
    try:
        models = await nebius.list_models()
    finally:
        await http.aclose()

    assert models == [{"id": "nvidia/Nemotron-3_5-Lightning"}]


async def test_embeddings_are_returned_in_index_order() -> None:
    payload = {
        "data": [
            {"index": 1, "embedding": [0.3, 0.4]},
            {"index": 0, "embedding": [0.1, 0.2]},
        ]
    }
    nebius, http = build_mock_client(lambda _r: httpx.Response(200, json=payload))
    try:
        vectors = await nebius.create_embeddings(input_texts=["a", "b"])
    finally:
        await http.aclose()

    assert vectors == [[0.1, 0.2], [0.3, 0.4]]


async def test_stream_yields_sse_data_lines() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        body = (
            'data: {"choices":[{"delta":{"content":"He"}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"llo"}}]}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    nebius, http = build_mock_client(handler)
    try:
        lines = [
            line
            async for line in nebius.stream_chat_completion(
                messages=[{"role": "user", "content": "hi"}]
            )
        ]
    finally:
        await http.aclose()

    assert lines[0].startswith("data: ")
    assert lines[-1] == "data: [DONE]"
