"""Async SQLAlchemy engine and session factory."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import AsyncAdaptedQueuePool

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


def build_engine(dsn: str | None = None, **overrides: Any) -> AsyncEngine:
    """Create the async engine used by the app and by Alembic."""
    url = dsn or settings.async_database_dsn
    options: dict[str, Any] = {
        "echo": settings.db_echo,
        "pool_pre_ping": settings.db_pool_pre_ping,
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
        "pool_recycle": settings.db_pool_recycle_seconds,
    }
    # SQLite (used by the test suite) does not accept pool sizing arguments.
    if url.startswith("sqlite"):
        options = {"echo": settings.db_echo}
    options.update(overrides)
    return create_async_engine(url, **options)


engine: AsyncEngine = build_engine()

SessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a request-scoped session."""
    async with SessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def dispose_engine() -> None:
    """Close pooled connections. Called on application shutdown."""
    await engine.dispose()
    logger.info("database connection pool disposed")


def pool_status() -> dict[str, Any]:
    """Return a JSON-friendly view of the engine's connection pool.

    Reports the pool implementation's own counters when available, and always
    includes the configured sizing so a misconfigured pool is visible in the
    logs even before the first checkout.
    """
    pool = engine.pool
    status: dict[str, Any] = {
        "pool_class": type(pool).__name__,
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
        "recycle_seconds": settings.db_pool_recycle_seconds,
        "pre_ping": settings.db_pool_pre_ping,
    }
    if isinstance(pool, AsyncAdaptedQueuePool):  # pragma: no branch - pool-specific
        status |= {
            "size": pool.size(),
            "checked_in": pool.checkedin(),
            "checked_out": pool.checkedout(),
            "overflow": pool.overflow(),
        }
    return status


async def warmup_pool() -> bool:
    """Establish the initial pooled connections and report readiness.

    Opening a connection at start-up moves the TCP/TLS/auth handshake off the
    first user request, so the first API call is not penalised by a cold pool.
    Retries wait a growing multiple of ``DB_CONNECT_BACKOFF_SECONDS`` (the delay
    is multiplied by the attempt number) bounded by ``DB_CONNECT_RETRIES``. The
    result is advisory: a database that is merely slow to boot should not take
    the whole API down, since ``/health/ready`` is the authoritative probe.
    """
    attempts = max(1, settings.db_connect_retries)
    for attempt in range(1, attempts + 1):
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception as exc:
            if attempt == attempts:
                message = f"database unreachable after {attempts} attempt(s): {exc}"
                if settings.db_fail_fast:
                    logger.error("%s", message)
                    raise
                logger.warning("%s; continuing to serve /health/ready as degraded", message)
                return False
            delay = settings.db_connect_backoff_seconds * attempt
            logger.warning(
                "database warm-up attempt %d/%d failed (%s); retrying in %.1fs",
                attempt,
                attempts,
                exc,
                delay,
            )
            await asyncio.sleep(delay)
        else:
            logger.info("database connection pool warm: %s", pool_status())
            return True
    return False  # pragma: no cover - unreachable; loop always returns above


async def check_database() -> bool:
    """Probe the database for the readiness probe."""
    from sqlalchemy import text

    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        logger.warning("Database readiness check failed: %s", exc)
        return False
    return True
