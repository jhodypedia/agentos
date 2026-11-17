"""Read-only aggregation for the dashboard.

Every number here comes from a query against persisted rows. Nothing is
simulated: an empty install reports zeros.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import Date, func, select, text
from sqlalchemy import cast as sql_cast
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import UnprocessableError
from app.db.enums import (
    AgentStatus,
    ApprovalStatus,
    CompetitionStatus,
    Severity,
    TaskStatus,
)
from app.db.models import (
    Agent,
    Approval,
    AuditLog,
    Competition,
    Finding,
    ModelUsage,
    Task,
)
from app.schemas.dashboard import (
    ActivityItem,
    DashboardSummary,
    UsagePoint,
    UsageSeries,
)


def _activity(row: AuditLog) -> ActivityItem:
    return ActivityItem(
        id=row.id,
        kind="audit",
        action=row.action,
        resource_type=row.resource_type,
        resource_id=row.resource_id,
        success=row.success,
        actor_user_id=row.actor_user_id,
        created_at=row.created_at,
    )


async def _grouped_counts(db: AsyncSession, column: Any) -> dict[str, int]:
    result = await db.execute(select(column, func.count()).group_by(column))
    return {str(key): int(count) for key, count in result.all()}


async def health(db: AsyncSession, settings: Settings) -> dict[str, str]:
    components: dict[str, str] = {}
    try:
        await db.execute(text("SELECT 1"))
        components["database"] = "ok"
    except Exception as exc:  # pragma: no cover - only on a broken database
        components["database"] = f"error: {type(exc).__name__}"

    import redis.asyncio as aioredis

    client = aioredis.Redis.from_url(
        settings.redis_url, socket_timeout=settings.redis_socket_timeout_seconds
    )
    try:
        await cast("Any", client.ping())
        components["redis"] = "ok"
    except Exception as exc:  # pragma: no cover - only without Redis
        components["redis"] = f"error: {type(exc).__name__}"
    finally:
        await client.aclose()
    return components


async def summary(
    db: AsyncSession, settings: Settings, *, activity_limit: int = 12
) -> DashboardSummary:
    agents_by_status = await _grouped_counts(db, Agent.status)
    tasks_by_status = await _grouped_counts(db, Task.status)

    approvals_pending = int(
        await db.scalar(
            select(func.count(Approval.id)).where(Approval.status == ApprovalStatus.PENDING)
        )
        or 0
    )
    open_competitions = int(
        await db.scalar(
            select(func.count(Competition.id)).where(
                Competition.status.not_in([CompetitionStatus.CLOSED, CompetitionStatus.ARCHIVED])
            )
        )
        or 0
    )
    findings_by_severity = await _grouped_counts(db, Finding.severity)

    usage_row = (
        await db.execute(
            select(
                func.coalesce(func.sum(ModelUsage.total_tokens), 0),
                func.coalesce(func.sum(ModelUsage.cost_usd), 0),
            )
        )
    ).one()

    audits = list(
        (
            await db.scalars(
                select(AuditLog).order_by(AuditLog.created_at.desc()).limit(activity_limit)
            )
        ).all()
    )

    return DashboardSummary(
        generated_at=datetime.now(UTC),
        health=await health(db, settings),
        agents_by_status=agents_by_status,
        tasks_by_status=tasks_by_status,
        agents_total=sum(agents_by_status.values()),
        tasks_total=sum(tasks_by_status.values()),
        approvals_pending=approvals_pending,
        open_competitions=open_competitions,
        findings_by_severity=findings_by_severity,
        total_tokens=int(usage_row[0]),
        total_cost_usd=Decimal(str(usage_row[1])),
        activity=[_activity(row) for row in audits],
        limits={
            "sandbox_enabled": settings.sandbox_enabled,
            "sandbox_image": settings.sandbox_image,
            "workspace_root": str(settings.workspace_root),
            "telegram_configured": settings.telegram_bot_token is not None,
            "makefun_configured": settings.makefun_api_key is not None,
        },
    )


async def usage_series(db: AsyncSession, *, days: int = 14) -> UsageSeries:
    if not 1 <= days <= 90:
        raise UnprocessableError("days must be between 1 and 90")
    # Start at the beginning of the first day in the window, not at this instant:
    # "the last 1 day" means today, and a since of `now` would exclude every row
    # written earlier today.
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    since = today - timedelta(days=days - 1)
    bucket = sql_cast(func.date_trunc("day", ModelUsage.created_at), Date).label("day")
    rows = (
        await db.execute(
            select(
                bucket,
                func.count(ModelUsage.id),
                func.coalesce(func.sum(ModelUsage.total_tokens), 0),
                func.coalesce(func.sum(ModelUsage.cost_usd), 0),
            )
            .where(ModelUsage.created_at >= since)
            .group_by(bucket)
            .order_by(bucket)
        )
    ).all()

    points = [
        UsagePoint(
            day=row[0].isoformat(),
            requests=int(row[1]),
            tokens=int(row[2]),
            cost_usd=Decimal(str(row[3])),
        )
        for row in rows
    ]
    return UsageSeries(
        days=days,
        points=points,
        total_requests=sum(point.requests for point in points),
        total_tokens=sum(point.tokens for point in points),
        total_cost_usd=sum((point.cost_usd for point in points), Decimal("0")),
    )


async def recent_audit(
    db: AsyncSession,
    *,
    resource_type: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[ActivityItem], int]:
    conditions = []
    if resource_type:
        conditions.append(AuditLog.resource_type == resource_type)
    total_statement = select(func.count(AuditLog.id))
    statement = select(AuditLog).order_by(AuditLog.created_at.desc())
    if conditions:
        total_statement = total_statement.where(*conditions)
        statement = statement.where(*conditions)
    total = int((await db.execute(total_statement)).scalar_one())
    rows = list((await db.scalars(statement.limit(limit).offset(offset))).all())
    return [_activity(row) for row in rows], total


ENUM_OPTIONS: dict[str, list[str]] = {
    "task_status": [status.value for status in TaskStatus],
    "agent_status": [status.value for status in AgentStatus],
    "severity": [status.value for status in Severity],
    "approval_status": [status.value for status in ApprovalStatus],
}
