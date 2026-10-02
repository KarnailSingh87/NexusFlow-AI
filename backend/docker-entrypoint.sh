#!/usr/bin/env sh
# ---------------------------------------------------------------------------
# Container entrypoint.
#
# Applies pending Alembic migrations before handing off to the server so that
# `docker compose up --build` yields a ready-to-use stack. Disable with
# RUN_MIGRATIONS=false when an external migration job owns the schema.
# ---------------------------------------------------------------------------
set -eu

if [ "${RUN_MIGRATIONS:-true}" = "true" ]; then
    echo "[entrypoint] waiting for database and applying migrations..."
    attempts=0
    until python -c "
import asyncio, sys
from sqlalchemy import text
from app.db.session import engine

async def main() -> None:
    async with engine.connect() as conn:
        await conn.execute(text('SELECT 1'))

try:
    asyncio.run(main())
except Exception:
    sys.exit(1)
" 2>/dev/null; do
        attempts=$((attempts + 1))
        if [ "$attempts" -ge "${DB_WAIT_ATTEMPTS:-30}" ]; then
            echo "[entrypoint] database not reachable after ${attempts} attempts" >&2
            exit 1
        fi
        sleep 2
    done

    alembic upgrade head
    echo "[entrypoint] migrations up to date"
fi

exec "$@"
