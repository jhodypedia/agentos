"""Approvals: human sign-off for sensitive orchestrated actions.

An approval is a first-class record, not a boolean on a task. Deciding one is
idempotent in the sense that a second decision is rejected explicitly, and every
decision is mirrored into the task's event log so the orchestrator can resume
from persisted state alone.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import Event, EventType, RedisEventBus
from app.core.exceptions import ConflictError, NotFoundError
from app.db.enums import ApprovalAction, ApprovalStatus
from app.db.models import Approval, Task
from app.services import tasks as task_service

DEFAULT_TTL_HOURS = 72


async def create_approval(
    db: AsyncSession,
    *,
    title: str,
    description: str = "",
    action_type: ApprovalAction = ApprovalAction.OTHER,
    task_id: UUID | None = None,
    step_id: UUID | None = None,
    finding_id: UUID | None = None,
    requested_by_agent_id: UUID | None = None,
    assigned_to_user_id: UUID | None = None,
    payload: dict[str, Any] | None = None,
    ttl_hours: int = DEFAULT_TTL_HOURS,
) -> Approval:
    if not title.strip():
        raise ConflictError("An approval needs a title")
    approval = Approval(
        title=title.strip()[:300],
        description=description,
        action_type=action_type,
        task_id=task_id,
        step_id=step_id,
        finding_id=finding_id,
        requested_by_agent_id=requested_by_agent_id,
        assigned_to_user_id=assigned_to_user_id,
        payload=payload or {},
        status=ApprovalStatus.PENDING,
        expires_at=datetime.now(UTC) + timedelta(hours=max(1, ttl_hours)),
    )
    db.add(approval)
    await db.flush()
    return approval


async def get_approval(db: AsyncSession, approval_id: UUID) -> Approval:
    approval = await db.get(Approval, approval_id)
    if approval is None:
        raise NotFoundError("Approval not found")
    return approval


async def list_approvals(
    db: AsyncSession,
    *,
    status: ApprovalStatus | None = None,
    task_id: UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Approval], int]:
    conditions = []
    if status is not None:
        conditions.append(Approval.status == status)
    if task_id is not None:
        conditions.append(Approval.task_id == task_id)

    statement = select(Approval).order_by(Approval.created_at.desc()).limit(limit).offset(offset)
    total_statement = select(func.count(Approval.id))
    if conditions:
        statement = statement.where(*conditions)
        total_statement = total_statement.where(*conditions)

    total = int((await db.execute(total_statement)).scalar_one())
    result = await db.execute(statement)
    return list(result.scalars()), total


def _expire_if_stale(approval: Approval, now: datetime) -> bool:
    if (
        approval.status == ApprovalStatus.PENDING
        and approval.expires_at is not None
        and approval.expires_at <= now
    ):
        approval.status = ApprovalStatus.EXPIRED
        return True
    return False


async def decide(
    db: AsyncSession,
    *,
    approval_id: UUID,
    approved: bool,
    decided_by_user_id: UUID,
    note: str = "",
    bus: RedisEventBus | None = None,
) -> Approval:
    approval = await get_approval(db, approval_id)
    now = datetime.now(UTC)

    if _expire_if_stale(approval, now):
        await db.flush()
        raise ConflictError("This approval has expired")

    if approval.status != ApprovalStatus.PENDING:
        raise ConflictError(f"This approval is already {approval.status.value}")

    approval.status = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
    approval.decided_by_user_id = decided_by_user_id
    approval.decided_at = now
    approval.decision_note = note[:5_000]

    if approval.task_id is not None:
        task = await db.get(Task, approval.task_id)
        if task is not None:
            await task_service.add_event(
                db,
                task_id=task.id,
                event_type="approval.decided",
                message=(f"Approval '{approval.title}' {'approved' if approved else 'rejected'}"),
                payload={
                    "approval_id": str(approval.id),
                    "approved": approved,
                    "note": note[:2_000],
                },
                agent_id=approval.requested_by_agent_id,
            )

    await db.flush()

    if bus is not None:
        with contextlib.suppress(Exception):
            await bus.publish(
                Event(
                    type=EventType.APPROVAL_DECIDED,
                    task_id=approval.task_id,
                    agent_id=approval.requested_by_agent_id,
                    payload={
                        "approval_id": str(approval.id),
                        "approved": approved,
                        "title": approval.title,
                    },
                )
            )

    return approval


async def cancel_pending_for_task(
    db: AsyncSession, *, task_id: UUID, note: str = "Task cancelled"
) -> int:
    """Cancel every pending approval attached to a task."""
    result = await db.execute(
        select(Approval).where(
            Approval.task_id == task_id, Approval.status == ApprovalStatus.PENDING
        )
    )
    count = 0
    now = datetime.now(UTC)
    for approval in result.scalars():
        approval.status = ApprovalStatus.CANCELLED
        approval.decided_at = now
        approval.decision_note = note
        count += 1
    await db.flush()
    return count


async def approved_tool_slugs(db: AsyncSession, *, task_id: UUID) -> set[str]:
    """Tool slugs a human has already approved for this task.

    The runtime consumes these as ``spec.approvals`` so a re-run of a paused
    task can execute exactly the high-risk tools that were signed off.
    """
    result = await db.execute(
        select(Approval).where(
            Approval.task_id == task_id,
            Approval.status == ApprovalStatus.APPROVED,
            Approval.action_type == ApprovalAction.RUN_HIGH_RISK_TOOL,
        )
    )
    slugs: set[str] = set()
    for approval in result.scalars():
        payload = approval.payload or {}
        tool = payload.get("tool")
        if isinstance(tool, str) and tool:
            slugs.add(tool)
    return slugs


async def has_pending_tool_approval(db: AsyncSession, *, task_id: UUID, tool: str) -> bool:
    """Whether an undecided request for the same tool already exists."""
    result = await db.execute(
        select(Approval.id).where(
            Approval.task_id == task_id,
            Approval.status == ApprovalStatus.PENDING,
            Approval.payload["tool"].astext == tool,
        )
    )
    return result.first() is not None
