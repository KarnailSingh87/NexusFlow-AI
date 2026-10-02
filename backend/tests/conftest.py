"""Shared pytest fixtures.

Environment variables are set *before* the application package is imported so
that ``Settings`` picks up test-safe values. No test in this suite touches a
real database or the Nebius API — provider traffic is served by
``httpx.MockTransport``.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable, Iterator

import httpx
import pytest

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("DEBUG", "false")
os.environ.setdefault("NEBIUS_API_KEY", "test-nebius-key")
os.environ.setdefault("NEBIUS_BASE_URL", "https://api.tokenfactory.nebius.com/v1")
os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://nexusflow:nexusflow@localhost:5432/nexusflow"
)
os.environ.setdefault("SECRET_KEY", "test-secret-key-only-for-local-tests-0123456789")
os.environ.setdefault("NEBIUS_MAX_RETRIES", "1")

from app.api.deps import get_nebius_client
from app.core.config import Settings, get_settings
from app.main import create_app
from app.services.nebius import NebiusClient
from fastapi import FastAPI, Request
from httpx import ASGITransport


@pytest.fixture(scope="session")
def app() -> FastAPI:
    get_settings.cache_clear()
    return create_app()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """In-process ASGI client — no socket bound, no network egress.

    ``ASGITransport`` does not fire lifespan events, so we enter the app's
    lifespan explicitly; otherwise ``app.state.nebius_client`` would never be
    populated and every dependency would resolve to a 503.
    """
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            yield ac


def build_mock_client(
    handler: Callable[[httpx.Request], httpx.Response],
    config: Settings | None = None,
) -> tuple[NebiusClient, httpx.AsyncClient]:
    """Build a ``NebiusClient`` backed by an in-memory mock transport."""
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url=(config or get_settings()).nebius_base_url,
    )
    return NebiusClient(config, client=http_client), http_client


@pytest.fixture
def override_nebius(app: FastAPI) -> Iterator[Callable[[NebiusClient], None]]:
    """Replace the shared Nebius client for the duration of one test."""

    def _install(nebius: NebiusClient) -> None:
        async def _get(_request: Request) -> NebiusClient:
            return nebius

        app.dependency_overrides[get_nebius_client] = _get

    yield _install
    app.dependency_overrides.clear()
