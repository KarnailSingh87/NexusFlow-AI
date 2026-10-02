"""Async SQLAlchemy engine and session factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


def build_engine(dsn: str | None = None, **overrides: Any) -> AsyncEngine:
    """Create the async engine used by the app and by Alembic."""
    url = dsn or settings.async_database_dsn
    options: dict[str, Any] = {
        "echo": settings.db_echo,
        "pool_pre_ping": True,
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
        "pool_recycle": 1800,
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
