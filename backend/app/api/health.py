"""Liveness and readiness probes plus service metadata."""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from app.api.deps import DbSession
from app.core.config import get_settings

router = APIRouter(tags=["system"])


@router.get("/health/live", summary="Liveness probe")
async def liveness() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready", summary="Readiness probe")
async def readiness(response: Response, db: DbSession) -> dict[str, Any]:
    components: dict[str, str] = {}

    try:
        await db.execute(text("SELECT 1"))
        components["database"] = "ok"
    except Exception as exc:
        components["database"] = f"error: {type(exc).__name__}"

    settings = get_settings()
    try:
        import redis.asyncio as aioredis

        client = aioredis.Redis.from_url(
            settings.redis_url, socket_timeout=settings.redis_socket_timeout_seconds
        )
        try:
            await cast("Any", client.ping())
            components["redis"] = "ok"
        finally:
            await client.aclose()
    except Exception as exc:
        components["redis"] = f"error: {type(exc).__name__}"

    healthy = all(value == "ok" for value in components.values())
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ok" if healthy else "degraded", "components": components}
