"""Alembic environment — async engine, DATABASE_URL injected from settings."""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from app.core.config import settings

# Importing the models module registers every table on Base.metadata.
from app.db import models  # noqa: F401  (side-effect import)
from app.db.base import Base
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy.pool import NullPool

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", settings.async_database_dsn)

target_metadata = Base.metadata


def _do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        include_schemas=False,
        # Required for autogenerate to detect UUID / JSONB changes.
        render_item=None,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a DB connection (``alembic upgrade --sql``)."""
    context.configure(
        url=settings.async_database_dsn,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
