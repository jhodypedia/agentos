"""Runtime settings that a database row can override.

`system_settings` holds operator-editable values seeded by the CLI. Some of them
also exist as environment settings, and the question "which wins" has to be
answered in exactly one place: a row edited in the dashboard takes effect for
the next run, while the environment value is the default when no row exists.

Only keys with a numeric or boolean meaning are read here, and every read
validates the value rather than trusting the JSONB payload.
"""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import SystemSetting

logger = structlog.get_logger("agentos.runtime_settings")

_WORKSPACE_QUOTA_KEY = "execution.workspace_quota_mb"
_SANDBOX_ENABLED_KEY = "execution.sandbox_enabled"
_MAX_PARALLEL_KEY = "orchestrator.max_parallel_tasks"


async def _raw_value(db: AsyncSession, key: str) -> Any:
    result = await db.execute(select(SystemSetting).where(SystemSetting.key == key))
    row = result.scalar_one_or_none()
    if row is None or not isinstance(row.value, dict):
        return None
    return row.value.get("value")


def _as_int(value: Any, *, minimum: int, maximum: int) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number < minimum or number > maximum:
        return None
    return number


async def workspace_quota_bytes(db: AsyncSession, settings: Settings) -> int:
    """Per-workspace disk quota, preferring the editable setting."""
    raw = await _raw_value(db, _WORKSPACE_QUOTA_KEY)
    megabytes = _as_int(raw, minimum=1, maximum=10 * 1024 * 1024)
    if megabytes is None:
        megabytes = settings.workspace_quota_mb
    elif raw is not None:
        logger.debug("workspace_quota_from_setting", megabytes=megabytes)
    return megabytes * 1024 * 1024


async def sandbox_enabled(db: AsyncSession, settings: Settings) -> bool:
    """Whether commands should be sandboxed.

    The environment setting can only ever *narrow* behaviour: if sandboxing is
    off in the environment, a database row cannot switch it back on, because the
    image and the Docker access are deployment facts, not operator preferences.
    """
    if not settings.sandbox_enabled:
        return False
    raw = await _raw_value(db, _SANDBOX_ENABLED_KEY)
    if isinstance(raw, bool):
        return raw
    return settings.sandbox_enabled


async def max_parallel_tasks(db: AsyncSession, settings: Settings) -> int | None:
    raw = await _raw_value(db, _MAX_PARALLEL_KEY)
    return _as_int(raw, minimum=1, maximum=64)
