"""Guard against schema drift between the ORM models and the Alembic migrations.

``alembic upgrade head --sql`` renders the whole schema without touching a
database, so this suite stays offline. Any table or column added to
``app/db/models.py`` without a matching migration shows up here.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

import pytest

# Importing the models module is what populates `Base.metadata`; without it the
# parametrised table list below would be empty at collection time.
from app.core.config import get_settings
from app.db import models as _models  # noqa: F401  (side-effect import)
from app.db.base import Base

BACKEND_ROOT = Path(__file__).resolve().parent.parent

_CREATE_TABLE = re.compile(r"CREATE TABLE (\w+) \((.*?)\n\)", re.DOTALL)
_COLUMN = re.compile(r"^\s*(\w+) ")
_CONSTRAINT_PREFIXES = ("CONSTRAINT", "UNIQUE", "PRIMARY", "FOREIGN", "CHECK")


@pytest.fixture(scope="module")
def migrated_schema() -> dict[str, set[str]]:
    """Render every migration to SQL and index the resulting columns per table."""
    # Alembic takes the DSN from settings; offline mode never opens a connection,
    # so a syntactically valid placeholder is enough.
    os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost:5432/nexusflow")
    get_settings.cache_clear()

    from alembic import command
    from alembic.config import Config

    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))

    buffer = StringIO()
    with redirect_stdout(buffer):
        command.upgrade(config, "head", sql=True)

    schema: dict[str, set[str]] = {}
    for table, body in _CREATE_TABLE.findall(buffer.getvalue()):
        if table == "alembic_version":  # Alembic's own bookkeeping table
            continue
        columns = set()
        for line in body.splitlines():
            if line.strip().startswith(_CONSTRAINT_PREFIXES):
                continue
            match = _COLUMN.match(line)
            if match:
                columns.add(match.group(1))
        schema[table] = columns
    return schema


def _model_tables() -> Iterator[str]:
    yield from sorted(Base.metadata.tables)


def test_migrations_create_every_model_table(migrated_schema: dict[str, set[str]]) -> None:
    """No table may exist in the ORM without a migration."""
    missing = set(Base.metadata.tables) - set(migrated_schema)

    assert not missing, f"tables missing from migrations: {sorted(missing)}"


def test_migrations_create_no_orphan_tables(migrated_schema: dict[str, set[str]]) -> None:
    """No table may exist in a migration without a model."""
    orphaned = set(migrated_schema) - set(Base.metadata.tables)

    assert not orphaned, f"tables with no model: {sorted(orphaned)}"


@pytest.mark.parametrize("table", list(_model_tables()))
def test_migration_columns_match_the_model(
    table: str, migrated_schema: dict[str, set[str]]
) -> None:
    """Every mapped column must be created by the migrations."""
    assert table in migrated_schema, f"{table} has no migration"

    expected = set(Base.metadata.tables[table].columns.keys())
    missing = expected - migrated_schema[table]

    assert not missing, f"{table}: columns missing from migrations: {sorted(missing)}"
