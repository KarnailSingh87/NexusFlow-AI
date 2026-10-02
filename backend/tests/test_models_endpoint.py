"""Tests for the model-discovery endpoints."""

from __future__ import annotations

import httpx

LIVE_MODELS = {
    "object": "list",
    "data": [
        {"id": "nvidia/Nemotron-3_5-Lightning", "owned_by": "nvidia", "created": 1},
        {"id": "meta-llama/Llama-3.3-70B-Instruct", "owned_by": "meta"},
        {"id": "nvidia/Nemotron-3-Ultra-550b-a55b", "owned_by": "nvidia"},
    ],
}


async def test_catalog_is_served_without_a_provider_call(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/api/v1/models/catalog")

    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "curated"
    ids = [m["id"] for m in body["data"]]
    assert "nvidia/Nemotron-3_5-Lightning" in ids


async def test_models_falls_back_to_curated_when_provider_fails(
    client: httpx.AsyncClient, override_nebius
) -> None:
    from tests.conftest import build_mock_client

    # A 500 from the provider must degrade to the bundled catalogue rather than
    # surface an error, so the UI always has something to render.
    nebius, http = build_mock_client(lambda _r: httpx.Response(500, json={"error": "boom"}))
    override_nebius(nebius)
    try:
        response = await client.get("/api/v1/models")
    finally:
        await http.aclose()

    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "curated"
    assert body["default_model"] == "nvidia/nemotron-3-super-120b-a12b"
    assert body["data"][0]["id"] == body["default_model"]


async def test_live_models_are_enriched_and_filtered(
    client: httpx.AsyncClient, override_nebius
) -> None:
    from tests.conftest import build_mock_client

    nebius, http = build_mock_client(lambda _r: httpx.Response(200, json=LIVE_MODELS))
    override_nebius(nebius)
    try:
        response = await client.get("/api/v1/models")
    finally:
        await http.aclose()

    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "live"
    # meta-llama is filtered out by nemotron_only=True
    ids = [m["id"] for m in body["data"]]
    assert "meta-llama/Llama-3.3-70B-Instruct" not in ids
    assert "nvidia/Nemotron-3-Ultra-550b-a55b" in ids

    lightning = next(m for m in body["data"] if m["id"] == "nvidia/Nemotron-3_5-Lightning")
    assert lightning["tier"] == "fast"
    assert lightning["context_tokens"] == 1_048_576
    assert lightning["curated"] is True


async def test_unknown_model_lookup_returns_404(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/models/nvidia/does-not-exist")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "HTTPError"


async def test_deprecated_model_is_flagged_with_replacement(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/api/v1/models/nvidia/Llama-3_1-Nemotron-Ultra-253B-v1")

    assert response.status_code == 200
    body = response.json()
    assert body["deprecated"] is True
    assert body["replacement"] == "nvidia/nemotron-3-super-120b-a12b"
