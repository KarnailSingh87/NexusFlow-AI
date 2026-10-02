"""Shared pytest fixtures.

Environment variables are set *before* the application package is imported so
that ``Settings`` picks up test-safe values. No test in this suite touches a
real database or the Nebius API — provider traffic is served by
``httpx.MockTransport``.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

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
# The suite never talks to a database, so the start-up pool warm-up must fail
# fast instead of retrying: a single attempt against the unreachable local DSN
# fails immediately, and `DB_FAIL_FAST=false` keeps the boot non-fatal.
os.environ.setdefault("DB_CONNECT_RETRIES", "1")
os.environ.setdefault("DB_CONNECT_BACKOFF_SECONDS", "0")

from app.api.deps import get_nebius_client
from app.core.config import Settings, get_settings
from app.db.models import Base
from app.db.session import get_session
from app.main import create_app
from app.services.jobs import JobWorker
from app.services.nebius import NebiusClient
from fastapi import FastAPI, Request
from httpx import ASGITransport
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


@pytest.fixture(scope="session")
def app() -> FastAPI:
    get_settings.cache_clear()
    return create_app()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """In-process ASGI client — no socket bound, no network egress.

    Deliberately function-scoped: entering the lifespan per test keeps every
    request on the same event loop as its start-up and shut-down, which is what
    lets the shared provider client's connection pool be closed on the loop that
    opened it. Broader scopes outlive their event loop and fail at teardown.

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


@pytest.fixture
async def db_session(app: FastAPI) -> AsyncIterator[AsyncSession]:
    """A real async session against a throwaway in-memory SQLite database.

    The rest of the suite never touches a database, but the ingestion endpoints
    genuinely need one — chunk persistence, ownership checks, and cascade
    deletes are the behaviour under test. An in-memory SQLite engine with
    ``foreign_keys=ON`` exercises those paths for real without a Postgres
    server, while keeping ``JSONVariant`` columns working the same way they do in
    production.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: sync.execute(text("PRAGMA foreign_keys=ON")))
        await connection.run_sync(Base.metadata.create_all)

    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        try:
            yield session
        finally:
            await session.close()
    await engine.dispose()


@pytest.fixture
def override_session(app: FastAPI) -> Callable[[AsyncSession], None]:
    """Point the ``get_session`` dependency at a test database."""

    def _install(session: AsyncSession) -> None:
        async def _get() -> AsyncIterator[AsyncSession]:
            yield session

        app.dependency_overrides[get_session] = _get

    return _install


@pytest.fixture
def settings() -> Settings:
    """The process settings, as a fixture so a test can derive a variant."""
    return get_settings()


@pytest.fixture
def upload_dir(tmp_path: Path) -> Path:
    """An isolated storage directory for uploads created during a test."""
    target = tmp_path / "uploads"
    target.mkdir()
    return target


@pytest.fixture
def ingest_settings(settings: Settings) -> Settings:
    """Settings pointed at the per-test storage directory and small limits."""
    return settings.model_copy(
        update={
            "upload_storage_dir": str(settings.upload_storage_dir),
            "clamav_host": "",
            "ingest_embed_chunks": False,
        }
    )


@pytest.fixture
async def queue_engine(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A session factory over a file-backed SQLite database for the job queue.

    Deliberately *not* ``:memory:``. An in-memory SQLite engine uses
    ``StaticPool``, so every session shares one connection and two concurrent
    workers interleave their transactions on it — which hides exactly the
    claiming races this suite exists to catch. A file-backed database gives each
    session its own connection and real locking, the way Postgres will in
    production.

    The worker opens sessions from a factory rather than sharing the request's
    session, so the factory must outlive any single session.
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'queue.db'}")

    # Foreign keys are per-connection in SQLite and off by default, so the pragma
    # has to be attached to every pooled connection. Setting it once on one
    # connection would leave ON DELETE CASCADE silently inert on the others, and
    # a cascade test would pass or fail depending on which connection it drew.
    @event.listens_for(engine.sync_engine, "connect")
    def _enable_foreign_keys(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield maker
    finally:
        await engine.dispose()


@pytest.fixture
def job_settings(settings: Settings) -> Settings:
    """Settings tuned so a queued job runs within a test rather than never."""
    return settings.model_copy(
        update={
            "jobs_enabled": True,
            "job_worker_concurrency": 2,
            "job_poll_interval": 0.01,
            "job_retry_backoff_seconds": 0.01,
            "job_stale_after_seconds": 5.0,
            "job_execution_timeout_seconds": 10.0,
            "job_drain_timeout_seconds": 5.0,
            "job_max_log_entries": 200,
            "ingest_embed_chunks": False,
        }
    )


class FakeEmbeddingClient:
    """Minimal stand-in for the Nebius client's embedding method."""

    def __init__(self, *, dimensions: int = 3) -> None:
        self.dimensions = dimensions
        self.calls: list[list[str]] = []

    async def create_embeddings(
        self, *, input_texts: list[str], model: str | None = None
    ) -> list[list[float]]:
        self.calls.append(list(input_texts))
        return [[0.1 * (index + 1)] * self.dimensions for index, _ in enumerate(input_texts)]


@pytest.fixture
def fake_client() -> FakeEmbeddingClient:
    """A client whose embeddings are deterministic and instant."""
    return FakeEmbeddingClient()


@pytest.fixture
async def running_worker(
    queue_engine: async_sessionmaker[AsyncSession], job_settings: Settings
) -> AsyncIterator[JobWorker]:
    """A started :class:`JobWorker` that is always stopped on teardown."""
    worker = JobWorker(queue_engine, config=job_settings, name="test-worker")
    await worker.start()
    try:
        yield worker
    finally:
        await worker.stop()


@pytest.fixture
def make_worker(
    queue_engine: async_sessionmaker[AsyncSession], job_settings: Settings
) -> Callable[..., JobWorker]:
    """Build a worker with a chosen config, for tests that need control."""

    def _make(**overrides: Any) -> JobWorker:
        return JobWorker(
            queue_engine,
            config=job_settings.model_copy(update=overrides) if overrides else job_settings,
            name="custom-worker",
        )

    return _make
