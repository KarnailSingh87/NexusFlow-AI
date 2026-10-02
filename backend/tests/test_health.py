"""Tests for health probes and the root redirect."""

from __future__ import annotations

import httpx


async def test_liveness_probe(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"]["service"] == "up"


async def test_readiness_probe_reports_each_dependency(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/health/ready")

    # 200 when both dependencies are up, 503 when the database is unreachable.
    assert response.status_code in (200, 503)
    checks = response.json()["checks"]
    assert set(checks) == {"database", "nebius_token_factory"}


async def test_root_redirects_to_docs(client: httpx.AsyncClient) -> None:
    response = await client.get("/", follow_redirects=False)

    assert response.status_code in (307, 302)
    assert response.headers["location"] == "/docs"


async def test_request_id_header_is_echoed(client: httpx.AsyncClient) -> None:
    response = await client.get("/health", headers={"X-Request-ID": "abc-123"})

    assert response.headers["X-Request-ID"] == "abc-123"
    assert response.headers["X-Process-Time-Ms"]
