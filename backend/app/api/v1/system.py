"""API metadata endpoint."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.core.config import get_settings
from app.core.events import EventType
from app.tools.builtin import BUILTIN_TOOLS

router = APIRouter(tags=["system"])


@router.get("/meta", summary="API metadata")
async def meta() -> dict[str, Any]:
    settings = get_settings()
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "environment": settings.environment,
        "api_prefix": settings.api_prefix,
        "phase": 2,
        "event_types": [event.value for event in EventType],
        "builtin_tools": [tool.slug for tool in BUILTIN_TOOLS],
    }
