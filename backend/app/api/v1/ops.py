"""Operational visibility: searchable logs and runtime resource reporting.

Two audiences:

* an operator chasing a failed task wants to search task events and audit rows
  by text, level, agent and time range, without a database client;
* a monitoring system wants the *shape* of the install — queue depth, stuck
  approvals, workspace disk usage, sandbox mode — in one cheap response.

Everything here is read-only and comes from persisted rows or live counters. No
number is estimated.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import DbSession, OperatorPrincipal, ViewerPrincipal
from app.core.config import Settings
from app.core.exceptions import UnprocessableError
from app.db.enums import ApprovalStatus, EventLevel, TaskStatus
from app.db.models import Approval, AuditLog, ModelUsage, Task, TaskEvent
from app.services import runtime_settings
from app.services.dashboard import health as service_health
from app.workspace.sandbox import docker_available

router = APIRouter(prefix="/ops", tags=["ops"])


class LogEntry(BaseModel):
    id: UUID
    source: str
    level: str
    event_type: str
    message: str
    task_id: UUID | None = None
    agent_id: UUID | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class LogPage(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[LogEntry] = Field(default_factory=list)


class ResourceUsage(BaseModel):
    workspace_root: str
    workspace_filesystem_free_bytes: int | None = None
    workspace_filesystem_total_bytes: int | None = None
    workspace_bytes: int = 0
    workspace_quota_bytes: int = 0
    database_tasks: int = 0
    running_tasks: int = 0
    queued_tasks: int = 0
    pending_approvals: int = 0
    oldest_pending_approval_seconds: int | None = None
    model_calls_24h: int = 0
    tokens_24h: int = 0
    cost_usd_24h: float = 0.0


class SandboxStatus(BaseModel):
    enabled: bool
    allow_host_execution: bool
    docker_available: bool
    image: str
    mode: str
    max_timeout_seconds: int
    memory_limit: str
    cpu_limit: float
    pids_limit: int


class OpsStatus(BaseModel):
    generated_at: datetime
    environment: str
    version: str
    rate_limit_enabled: bool
    rate_limit_requests: int
    rate_limit_window_seconds: int
    metrics_enabled: bool
    metrics_token_required: bool
    retention: dict[str, int]
    sandbox: SandboxStatus
    resources: ResourceUsage
    components: dict[str, str]


@router.get("/status", response_model=OpsStatus, summary="Runtime status and resource usage")
async def ops_status(
    principal: ViewerPrincipal,
    db: DbSession,
    request: Request,
) -> OpsStatus:
    settings: Settings = request.app.state.settings
    quota_bytes = await runtime_settings.workspace_quota_bytes(db, settings)
    sandbox_enabled = await runtime_settings.sandbox_enabled(db, settings)
    docker_up = await docker_available()
    components = await service_health(db, settings)

    resources = await _resource_usage(db, settings, quota_bytes=quota_bytes)
    return OpsStatus(
        generated_at=datetime.now(UTC),
        environment=settings.environment,
        version=settings.app_version,
        rate_limit_enabled=settings.rate_limit_enabled,
        rate_limit_requests=settings.rate_limit_requests,
        rate_limit_window_seconds=settings.rate_limit_window_seconds,
        metrics_enabled=settings.metrics_enabled,
        metrics_token_required=settings.metrics_token is not None,
        retention={
            "task_events_days": settings.retention_task_events_days,
            "audit_days": settings.retention_audit_days,
            "model_usage_days": settings.retention_model_usage_days,
        },
        sandbox=SandboxStatus(
            enabled=sandbox_enabled,
            allow_host_execution=settings.sandbox_allow_host_execution,
            docker_available=docker_up,
            image=settings.sandbox_image,
            mode="docker" if sandbox_enabled and docker_up else "host",
            max_timeout_seconds=settings.sandbox_max_timeout_seconds,
            memory_limit=settings.sandbox_memory_limit,
            cpu_limit=settings.sandbox_cpu_limit,
            pids_limit=settings.sandbox_pids_limit,
        ),
        resources=resources,
        components=components,
    )


async def _resource_usage(
    db: AsyncSession, settings: Settings, *, quota_bytes: int
) -> ResourceUsage:
    root = str(settings.workspace_root)
    free, total = await _filesystem_usage(root)
    workspace_bytes = await _workspace_bytes(root, quota_bytes)

    task_counts = {
        str(getattr(value, "value", value)): int(count)
        for value, count in await db.execute(
            select(Task.status, func.count()).select_from(Task).group_by(Task.status)
        )
    }
    pending = int(
        await db.scalar(
            select(func.count())
            .select_from(Approval)
            .where(Approval.status == ApprovalStatus.PENDING)
        )
        or 0
    )
    oldest = await db.scalar(
        select(func.min(Approval.created_at)).where(Approval.status == ApprovalStatus.PENDING)
    )
    stale_seconds: int | None = None
    if isinstance(oldest, datetime):
        stale_seconds = max(0, int((datetime.now(UTC) - _aware(oldest)).total_seconds()))

    since = datetime.now(UTC) - timedelta(hours=24)
    calls, tokens, cost = (
        await db.execute(
            select(
                func.count(ModelUsage.id),
                func.coalesce(func.sum(ModelUsage.total_tokens), 0),
                func.coalesce(func.sum(ModelUsage.cost_usd), 0),
            ).where(ModelUsage.created_at >= since)
        )
    ).one()

    return ResourceUsage(
        workspace_root=root,
        workspace_filesystem_free_bytes=free,
        workspace_filesystem_total_bytes=total,
        workspace_bytes=workspace_bytes,
        workspace_quota_bytes=quota_bytes,
        database_tasks=sum(task_counts.values()),
        running_tasks=task_counts.get(TaskStatus.RUNNING.value, 0),
        queued_tasks=task_counts.get(TaskStatus.QUEUED.value, 0),
        pending_approvals=pending,
        oldest_pending_approval_seconds=stale_seconds,
        model_calls_24h=int(calls),
        tokens_24h=int(tokens),
        cost_usd_24h=float(cost),
    )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def _filesystem_usage(root: str) -> tuple[int | None, int | None]:
    def _stat() -> tuple[int | None, int | None]:
        try:
            usage = shutil.disk_usage(root)
        except OSError:
            return None, None
        return usage.free, usage.total

    return await asyncio.to_thread(_stat)


async def _workspace_bytes(root: str, limit: int) -> int:
    def _walk() -> int:
        total = 0
        for current, _dirs, files in os.walk(root):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(current, name))
                except OSError:
                    continue
                if total > limit:
                    return total
        return total

    return await asyncio.to_thread(_walk)


def _log_from_task_event(row: TaskEvent) -> LogEntry:
    level = getattr(row.level, "value", row.level)
    return LogEntry(
        id=row.id,
        source="task",
        level=str(level),
        event_type=row.event_type,
        message=row.message,
        task_id=row.task_id,
        agent_id=row.agent_id,
        payload=row.payload or {},
        created_at=row.created_at,
    )


def _log_from_audit(row: AuditLog) -> LogEntry:
    return LogEntry(
        id=row.id,
        source="audit",
        level="info" if row.success else "warning",
        event_type=row.action,
        message=f"{row.action} on {row.resource_type}"
        + (f" {row.resource_id}" if row.resource_id else ""),
        task_id=None,
        agent_id=row.actor_agent_id,
        payload=dict(row.meta or {}),
        created_at=row.created_at,
    )


_LEVELS = {level.value: level for level in EventLevel}


@router.get("/logs", response_model=LogPage, summary="Search task events and audit rows")
async def search_logs(
    principal: OperatorPrincipal,
    db: DbSession,
    q: str | None = Query(default=None, max_length=200, description="Substring match"),
    level: str | None = Query(default=None, description="Minimum severity"),
    source: str | None = Query(default=None, pattern="^(task|audit)$"),
    task_id: UUID | None = Query(default=None),
    agent_id: UUID | None = Query(default=None),
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> LogPage:
    """Union of task events and audit entries, newest first.

    The two tables are searched separately and merged in Python rather than with
    a SQL UNION: they have different columns, and a page that mixes sources has
    to be ordered by ``created_at`` across both, which the merge does exactly.
    """
    if level is not None and level not in _LEVELS:
        raise UnprocessableError(
            f"Unknown log level '{level}'", details={"allowed": sorted(_LEVELS)}
        )

    conditions = []
    if task_id is not None:
        conditions.append(TaskEvent.task_id == task_id)
    if agent_id is not None:
        conditions.append(TaskEvent.agent_id == agent_id)
    if since is not None:
        conditions.append(TaskEvent.created_at >= since)
    if until is not None:
        conditions.append(TaskEvent.created_at <= until)
    if q:
        pattern = f"%{q}%"
        conditions.append(
            or_(
                TaskEvent.message.ilike(pattern),
                TaskEvent.event_type.ilike(pattern),
            )
        )
    # Audit rows do not carry a level of their own; _log_from_audit derives
    # "info" for a success and "warning" for a failure. Filtering only the task
    # side therefore let every audit row through an `error` filter, which made
    # the filter look broken to anyone who tried it. Resolve the threshold first
    # and apply it to both sides.
    level_threshold = _LEVELS[level] if level is not None else None
    audit_levels = [
        EventLevel.INFO,
        EventLevel.WARNING,
    ]
    if level_threshold is not None:
        allowed_values = [item.value for item in EventLevel]
        threshold_index = allowed_values.index(level_threshold.value)
        conditions.append(
            TaskEvent.level.in_([EventLevel(value) for value in allowed_values[threshold_index:]])
        )
        audit_levels = [
            candidate
            for candidate in audit_levels
            if allowed_values.index(candidate.value) >= threshold_index
        ]

    task_query = select(TaskEvent).where(*conditions) if conditions else select(TaskEvent)

    audit_conditions = []
    if since is not None:
        audit_conditions.append(AuditLog.created_at >= since)
    if until is not None:
        audit_conditions.append(AuditLog.created_at <= until)
    if q:
        pattern = f"%{q}%"
        audit_conditions.append(
            or_(
                AuditLog.action.ilike(pattern),
                AuditLog.resource_type.ilike(pattern),
                AuditLog.resource_id.ilike(pattern),
            )
        )
    if level_threshold is EventLevel.WARNING:
        # "warning" and above on the task side maps to failed audit rows only;
        # for "error" and higher `audit_levels` is empty and the audit source is
        # dropped below, because no audit row can reach those levels.
        audit_conditions.append(AuditLog.success.is_(False))

    audit_query = (
        select(AuditLog).where(*audit_conditions) if audit_conditions else select(AuditLog)
    )

    # Task events can be narrowed by task and agent, audit rows cannot — so those
    # filters select the task source and exclude the audit one. Filtering the
    # task_id must not drop the task source itself: that made every task-scoped
    # query return an empty page.
    want_tasks = source in (None, "task")
    want_audit = (
        source in (None, "audit")
        and task_id is None
        and agent_id is None
        and (level_threshold is None or bool(audit_levels))
    )

    entries: list[LogEntry] = []
    if want_tasks:
        task_rows = (
            await db.execute(task_query.order_by(TaskEvent.created_at.desc()).limit(offset + limit))
        ).scalars()
        entries.extend(_log_from_task_event(row) for row in task_rows)
    if want_audit:
        audit_rows = (
            await db.execute(audit_query.order_by(AuditLog.created_at.desc()).limit(offset + limit))
        ).scalars()
        entries.extend(_log_from_audit(row) for row in audit_rows)

    entries.sort(key=lambda entry: entry.created_at, reverse=True)

    total = 0
    if want_tasks:
        total += int(
            await db.scalar(
                select(func.count()).select_from(TaskEvent).where(*conditions)
                if conditions
                else select(func.count()).select_from(TaskEvent)
            )
            or 0
        )
    if want_audit:
        total += int(
            await db.scalar(
                select(func.count()).select_from(AuditLog).where(*audit_conditions)
                if audit_conditions
                else select(func.count()).select_from(AuditLog)
            )
            or 0
        )

    return LogPage(total=total, limit=limit, offset=offset, items=entries[offset : offset + limit])
