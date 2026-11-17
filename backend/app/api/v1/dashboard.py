"""Dashboard aggregates and the live event stream."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any, cast

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from app.api.deps import (
    DbSession,
    OperatorPrincipal,
    ViewerPrincipal,
    principal_from_request,
    principal_from_token,
)
from app.core.events import Event, RedisEventBus
from app.core.exceptions import PermissionDeniedError
from app.db.enums import UserRole
from app.schemas.dashboard import (
    AuditPage,
    DashboardSummary,
    EventPublic,
    UsageSeries,
)
from app.services import dashboard as dashboard_service

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/summary", response_model=DashboardSummary, summary="Dashboard aggregates")
async def dashboard_summary(
    principal: ViewerPrincipal,
    db: DbSession,
    request: Request,
    activity_limit: int = Query(default=12, ge=1, le=50),
) -> DashboardSummary:
    return await dashboard_service.summary(
        db, request.app.state.settings, activity_limit=activity_limit
    )


@router.get("/usage", response_model=UsageSeries, summary="Daily token and cost usage")
async def dashboard_usage(
    principal: ViewerPrincipal,
    db: DbSession,
    days: int = Query(default=14, ge=1, le=90),
) -> UsageSeries:
    return await dashboard_service.usage_series(db, days=days)


@router.get("/activity", response_model=AuditPage, summary="Audit-backed activity feed")
async def dashboard_activity(
    principal: OperatorPrincipal,
    db: DbSession,
    resource_type: str | None = Query(default=None, max_length=80),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> AuditPage:
    items, total = await dashboard_service.recent_audit(
        db, resource_type=resource_type, limit=limit, offset=offset
    )
    return AuditPage(total=total, limit=limit, offset=offset, items=items)


@router.get("/events", response_model=list[EventPublic], summary="Recent domain events")
async def recent_events(
    principal: OperatorPrincipal,
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[EventPublic]:
    bus: RedisEventBus = request.app.state.event_bus
    events = await bus.recent(limit)
    return [
        EventPublic(
            id=event.id,
            type=event.type.value,
            occurred_at=event.occurred_at,
            source=event.source,
            task_id=event.task_id,
            agent_id=event.agent_id,
            payload=event.payload,
        )
        for event in events
    ]


def _frame(event: Event) -> str:
    return f"id: {event.id}\nevent: {event.type.value}\ndata: {event.model_dump_json()}\n\n"


async def _stream(request: Request, bus: RedisEventBus) -> AsyncIterator[str]:
    """Forward Redis Pub/Sub events until the client disconnects.

    A comment is emitted every 20 seconds so idle proxies do not drop the
    connection, and the client is told to reconnect with the last event id.
    """
    pubsub = bus.client.pubsub()
    await pubsub.subscribe(bus.channel)
    try:
        yield ": connected\n\n"
        while True:
            if await request.is_disconnected():
                return
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=20.0)
            if message is None:
                yield f"retry: 3000\n: keepalive {json.dumps({'ok': True})}\n\n"
                continue
            raw = message.get("data")
            if not isinstance(raw, str):
                continue
            try:
                event = Event.model_validate_json(raw)
            except ValueError:
                continue
            yield _frame(event)
    except asyncio.CancelledError:  # pragma: no cover - client aborted
        raise
    finally:
        with contextlib.suppress(Exception):
            await pubsub.unsubscribe(bus.channel)
        with contextlib.suppress(Exception):
            await cast("Any", pubsub).aclose()


@router.get("/stream", summary="Server-sent events for live task and agent activity")
async def event_stream(
    request: Request,
    db: DbSession,
    token: str | None = Query(
        default=None,
        description="Access token for clients that cannot set headers (EventSource)",
    ),
) -> Any:
    """Stream domain events.

    ``EventSource`` cannot set an Authorization header, so this one route also
    accepts the JWT as a query parameter. It is verified exactly like a bearer
    token and never logged.
    """
    if token is not None:
        principal = await principal_from_token(token, db)
    else:
        principal = await principal_from_request(request, db)
    if principal.role not in {UserRole.OPERATOR, UserRole.ADMIN, UserRole.OWNER}:
        raise PermissionDeniedError("This action requires the operator role or higher")
    request.state.principal = principal

    bus: RedisEventBus = request.app.state.event_bus
    return StreamingResponse(
        _stream(request, bus),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
