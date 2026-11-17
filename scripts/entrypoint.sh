#!/usr/bin/env bash
set -euo pipefail

# Wait for PostgreSQL, then optionally apply migrations, then start the
# requested process.
#
# Migration ownership: only the API applies migrations. Every service shares
# this entrypoint, and if the worker and the API both ran `alembic upgrade head`
# on a fresh volume they would race and one would fail on a half-applied schema.
# Services that must not migrate set AGENTOS_RUN_MIGRATIONS=false.
: "${DATABASE_URL:?DATABASE_URL is required}"

python - <<'PY'
import asyncio
import os
import sys

import asyncpg
from sqlalchemy.engine import make_url


async def wait_for_db() -> None:
    url = make_url(os.environ["DATABASE_URL"])
    for attempt in range(30):
        try:
            conn = await asyncpg.connect(
                host=url.host,
                port=url.port or 5432,
                user=url.username,
                password=url.password,
                database=url.database,
                timeout=3,
            )
            await conn.close()
            return
        except Exception as exc:  # noqa: BLE001
            print(f"database not ready ({attempt + 1}/30): {exc}", file=sys.stderr)
            await asyncio.sleep(2)
    raise SystemExit("database did not become ready in time")


asyncio.run(wait_for_db())
PY

if [ "${AGENTOS_RUN_MIGRATIONS:-true}" = "true" ]; then
    alembic upgrade head
else
    echo "skipping migrations (AGENTOS_RUN_MIGRATIONS=false); the API owns them"
fi

exec "$@"
