"""Prometheus scrape endpoint.

Two kinds of metric live side by side here:

* counters and histograms accumulated in-process by the middleware, the runtime
  and the provider layer;
* gauges that describe *current* database state (tasks per status, agents per
  status, pending approvals). Those are refreshed with one query at scrape time
  rather than being incremented from every write path, which keeps them correct
  even for rows changed outside the API (a migration, a manual SQL fix).

The endpoint is deliberately outside ``API_PREFIX`` and exempt from rate
limiting: a scraper that gets a 429 during an incident is worse than useless.
"""

from __future__ import annotations

import hmac

import structlog
from fastapi import APIRouter, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.metrics import (
    AGENTS_BY_STATUS,
    APPROVALS_PENDING,
    TASKS_BY_STATUS,
    registry,
)
from app.db.enums import AgentStatus, ApprovalStatus, TaskStatus
from app.db.models import Agent, Approval, Task
from app.db.session import get_session_factory

logger = structlog.get_logger("agentos.metrics")

router = APIRouter(tags=["system"])

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


async def refresh_state_gauges(db: AsyncSession) -> None:
    """Rebuild the database-backed gauges from real rows."""
    task_rows = await db.execute(
        select(Task.status, func.count()).select_from(Task).group_by(Task.status)
    )
    tasks = {str(getattr(value, "value", value)): int(count) for value, count in task_rows}
    for task_status in TaskStatus:
        TASKS_BY_STATUS.set(tasks.get(task_status.value, 0), status=task_status.value)

    agent_rows = await db.execute(
        select(Agent.status, func.count()).select_from(Agent).group_by(Agent.status)
    )
    agents = {str(getattr(value, "value", value)): int(count) for value, count in agent_rows}
    for agent_status in AgentStatus:
        AGENTS_BY_STATUS.set(agents.get(agent_status.value, 0), status=agent_status.value)

    pending = await db.scalar(
        select(func.count()).select_from(Approval).where(Approval.status == ApprovalStatus.PENDING)
    )
    APPROVALS_PENDING.set(int(pending or 0))


def _authorised(request: Request, settings: Settings) -> bool:
    token = settings.metrics_token
    if token is None:
        return True
    raw = token.get_secret_value()
    header = request.headers.get("authorization", "")
    scheme, _, provided = header.partition(" ")
    if scheme.lower() != "bearer":
        return False
    return hmac.compare_digest(provided.strip().encode(), raw.encode())


@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request, response: Response) -> Response:
    settings: Settings = request.app.state.settings
    if not settings.metrics_enabled:
        return Response(status_code=404)
    if not _authorised(request, settings):
        return Response(
            status_code=401,
            content="metrics require a bearer token\n",
            media_type="text/plain",
            headers={"WWW-Authenticate": 'Bearer realm="metrics"'},
        )

    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        factory = get_session_factory(settings)
    try:
        async with factory() as db:
            await refresh_state_gauges(db)
    except Exception as exc:  # pragma: no cover - database outage during a scrape
        # Serve the in-process metrics anyway: a partially useful scrape beats a
        # failed one. The state gauges keep their previous values.
        logger.warning("metrics_state_refresh_failed", error=type(exc).__name__)

    return Response(content=registry.render(), media_type=CONTENT_TYPE)
