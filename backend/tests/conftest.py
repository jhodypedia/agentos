"""Test configuration and fixtures.

Unit tests run without external services. Integration tests require PostgreSQL
(and Redis for readiness/event tests); they are skipped automatically when the
services are unreachable.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

BACKEND_DIR = Path(__file__).resolve().parents[1]
TEST_DATABASE_URL = os.environ.get(
    "AGENTOS_TEST_DATABASE_URL",
    "postgresql+asyncpg://agentos:agentos@localhost:5432/agentos_test",
)
TEST_REDIS_URL = os.environ.get("AGENTOS_TEST_REDIS_URL", "redis://localhost:6379/15")

os.environ["ENVIRONMENT"] = "testing"
os.environ["SECRET_KEY"] = "test-secret-key-that-is-definitely-long-enough-1234567890"
os.environ["ENCRYPTION_KEY"] = "NCDnI89x2g-H2KQgWEKrJtxEEJXV_W-JCGDiaWCsee8="
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["REDIS_URL"] = TEST_REDIS_URL
os.environ["LOG_LEVEL"] = "WARNING"
os.environ["LOG_FORMAT"] = "console"
os.environ["BOOTSTRAP_ADMIN_EMAIL"] = ""
os.environ["BOOTSTRAP_ADMIN_PASSWORD"] = ""
# Skills and workspaces live under writable throwaway roots so tests never touch
# /data, cannot interfere with each other, and do not depend on a developer's
# .env: without this the default WORKSPACE_ROOT (/data/workspaces) fails with a
# permission error on a machine that does not have it, which is every CI runner.
TEST_SKILLS_ROOT = Path(tempfile.mkdtemp(prefix="agentos-skills-"))
os.environ["SKILLS_ROOT"] = str(TEST_SKILLS_ROOT)
TEST_WORKSPACE_ROOT = Path(tempfile.mkdtemp(prefix="agentos-workspaces-"))
os.environ["WORKSPACE_ROOT"] = str(TEST_WORKSPACE_ROOT)


def _url_parts() -> tuple[str, int, str, str, str]:
    url = make_url(TEST_DATABASE_URL)
    return (
        url.host or "localhost",
        url.port or 5432,
        url.username or "agentos",
        url.password or "agentos",
        url.database or "agentos_test",
    )


async def _ensure_database() -> None:
    host, port, user, password, database = _url_parts()
    conn = await asyncpg.connect(
        host=host, port=port, user=user, password=password, database="postgres"
    )
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", database)
        if not exists:
            await conn.execute(f'CREATE DATABASE "{database}"')
    finally:
        await conn.close()


def _postgres_available() -> bool:
    host, port, user, password, _database = _url_parts()

    async def _check() -> None:
        conn = await asyncpg.connect(
            host=host, port=port, user=user, password=password, database="postgres", timeout=3
        )
        await conn.close()

    try:
        asyncio.run(_check())
        return True
    except Exception:
        return False


def _redis_available() -> bool:
    async def _check() -> None:
        import redis.asyncio as aioredis

        client = aioredis.Redis.from_url(TEST_REDIS_URL, socket_timeout=2)
        try:
            await client.ping()
        finally:
            await client.aclose()

    try:
        asyncio.run(_check())
        return True
    except Exception:
        return False


DB_AVAILABLE = _postgres_available()
REDIS_AVAILABLE = _redis_available()


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if not DB_AVAILABLE:
        skip_db = pytest.mark.skip(reason="PostgreSQL is not available")
        for item in items:
            if "integration" in item.keywords:
                item.add_marker(skip_db)
    if not REDIS_AVAILABLE:
        skip_redis = pytest.mark.skip(reason="Redis is not available")
        for item in items:
            if "redis" in item.keywords:
                item.add_marker(skip_redis)


@pytest.fixture(scope="session", autouse=True)
def prepare_database() -> Iterator[None]:
    if not DB_AVAILABLE:
        yield None
        return

    asyncio.run(_ensure_database())
    env = {
        **os.environ,
        "DATABASE_URL": TEST_DATABASE_URL,
        "ENVIRONMENT": "testing",
        "SECRET_KEY": os.environ["SECRET_KEY"],
        "ENCRYPTION_KEY": os.environ["ENCRYPTION_KEY"],
    }
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"alembic upgrade failed:\n{result.stdout}\n{result.stderr}")
    yield None
    shutil.rmtree(TEST_SKILLS_ROOT, ignore_errors=True)
    shutil.rmtree(TEST_WORKSPACE_ROOT, ignore_errors=True)


async def _truncate_all() -> None:
    import app.db.models  # noqa: F401  (registers every table on Base.metadata)
    from app.db.base import Base
    from app.db.session import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        names = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
        await session.execute(text(f"TRUNCATE TABLE {names} RESTART IDENTITY CASCADE"))
        await session.commit()


@pytest.fixture(autouse=True)
async def clean_database(request: pytest.FixtureRequest) -> AsyncIterator[None]:
    if DB_AVAILABLE and "integration" in request.keywords:
        await _truncate_all()
        yield
        await _truncate_all()
    else:
        yield


@pytest.fixture
async def app_client() -> AsyncIterator[AsyncClient]:
    from app.main import create_app

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    from app.db.session import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        yield session
