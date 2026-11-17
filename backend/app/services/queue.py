"""Durable task queue: claim, heartbeat, release, reap.

The queue lives in the ``tasks`` table rather than in Redis. Redis holds the
event stream and the rate-limit counters, but it is not the source of truth for
work: a flushed Redis would lose queued tasks, whereas a row survives a restart
and is visible to the dashboard. Claiming is a single ``UPDATE ... RETURNING``
with a ``SKIP LOCKED``-style guard, so two workers polling at the same instant
cannot both pick the same row.

A claim is a *lease*, not a flag:

* ``claimed_by`` names the worker holding it;
* ``lease_expires_at`` is when the claim goes stale.

The worker heartbeats by extending the lease while it runs. If the process dies
the lease simply lapses, :func:`reap_expired` puts the task back in the queue,
and another worker retries it — subject to ``attempts``/``max_attempts``, so a
task that reliably crashes the worker eventually fails instead of looping.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import structlog
from sqlalchemy import or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.enums import TERMINAL_TASK_STATUSES, TaskStatus
from app.db.models import Task, TaskDependency
from app.services import tasks as task_service

logger = structlog.get_logger("agentos.queue")

CLAIMABLE_STATUSES = (TaskStatus.PENDING, TaskStatus.QUEUED)


def _now() -> datetime:
    return datetime.now(UTC)


async def _unmet_dependencies(db: AsyncSession, task_id: UUID) -> bool:
    """True when the task still waits on another task that has not succeeded.

    A dependency is satisfied by any terminal status other than ``failed``: a
    cancelled dependency must not block its dependants forever, but a failed one
    must, because a step whose input never materialised will only fail again.
    """
    rows = (
        await db.execute(
            select(TaskDependency.depends_on_task_id)
            .join(Task, Task.id == TaskDependency.depends_on_task_id)
            .where(
                TaskDependency.task_id == task_id,
                Task.status.not_in(TERMINAL_TASK_STATUSES),
            )
        )
    ).scalars()
    return rows.first() is not None


async def claim_next(
    db: AsyncSession,
    *,
    worker_id: str,
    lease_seconds: int,
    settings: Settings,
) -> Task | None:
    """Atomically take the highest-priority claimable task, or return ``None``.

    The candidate rows are read with ``FOR UPDATE SKIP LOCKED`` so a second
    worker moves on instead of waiting. A row already held under a live lease is
    excluded by the ``lease_expires_at`` predicate, which is what lets a
    replacement worker pick up after a crash without a separate reaper pass.
    """
    now = _now()
    lease_until = now + timedelta(seconds=lease_seconds)

    statement = (
        select(Task)
        .where(
            Task.status.in_(CLAIMABLE_STATUSES),
            or_(Task.lease_expires_at.is_(None), Task.lease_expires_at < now),
            or_(Task.scheduled_at.is_(None), Task.scheduled_at <= now),
        )
        .order_by(Task.priority.asc(), Task.created_at.asc())
        .limit(5)
        .with_for_update(skip_locked=True)
    )
    candidates = list((await db.execute(statement)).scalars())

    for task in candidates:
        if await _unmet_dependencies(db, task.id):
            logger.debug("task_waiting_on_dependency", task_id=str(task.id))
            continue
        if task.attempts >= task.max_attempts:
            # It was claimed before and used every attempt; parking it as failed
            # is honest and stops it from being re-examined on every poll.
            task.error = f"Task exhausted its {task.max_attempts} attempt(s)"
            await task_service.transition_task(
                db,
                task,
                TaskStatus.FAILED,
                message=task.error,
                payload={"attempts": task.attempts, "max_attempts": task.max_attempts},
            )
            await db.commit()
            logger.warning("task_attempts_exhausted", task_id=str(task.id))
            continue

        task.claimed_by = worker_id
        task.lease_expires_at = lease_until
        if task.status == TaskStatus.PENDING:
            await task_service.transition_task(
                db,
                task,
                TaskStatus.QUEUED,
                message=f"Task claimed by worker '{worker_id}'",
                payload={"worker": worker_id},
            )
        await db.commit()
        await db.refresh(task)
        logger.info(
            "task_claimed",
            task_id=str(task.id),
            worker=worker_id,
            lease_seconds=lease_seconds,
        )
        return task

    await db.rollback()
    return None


async def heartbeat(db: AsyncSession, *, task_id: UUID, worker_id: str, lease_seconds: int) -> bool:
    """Extend a lease the caller still owns.

    Returns ``False`` when the lease was lost — the task was reassigned, expired
    and re-claimed, or cancelled — which is the worker's signal to stop working
    on it rather than write results nobody owns any more.
    """
    result = await db.execute(
        update(Task)
        .where(Task.id == task_id, Task.claimed_by == worker_id)
        .values(lease_expires_at=_now() + timedelta(seconds=lease_seconds))
    )
    await db.commit()
    return bool(cast("CursorResult[Any]", result).rowcount)


async def release(
    db: AsyncSession,
    *,
    task_id: UUID,
    worker_id: str,
    requeue: bool,
    reason: str = "",
) -> None:
    """Give up a lease, requeueing the task or failing it.

    ``requeue=False`` means the failure is not worth another attempt (a
    configuration error, say). Clearing the lease alone would leave the row in
    ``running`` with nothing that could ever pick it up — the reaper only looks
    at live leases — so that path parks the task as failed instead.
    """
    task = await db.get(Task, task_id)
    if task is None or task.claimed_by != worker_id:
        return
    task.claimed_by = None
    task.lease_expires_at = None
    if not requeue and task.status not in TERMINAL_TASK_STATUSES:
        task.error = reason or "Worker gave up on the task"
        await task_service.transition_task(
            db,
            task,
            TaskStatus.FAILED,
            message=task.error,
            payload={"worker": worker_id, "attempts": task.attempts},
        )
    elif requeue and task.status not in TERMINAL_TASK_STATUSES:
        task.attempts += 1
        if task.attempts < task.max_attempts:
            if task.status in {TaskStatus.RUNNING, TaskStatus.QUEUED}:
                task.status = TaskStatus.QUEUED
            await task_service.add_event(
                db,
                task_id=task.id,
                event_type="task.requeued",
                message=reason or f"Requeued after worker '{worker_id}' released it",
                payload={"worker": worker_id, "attempt": task.attempts},
            )
        else:
            task.error = reason or "Worker released the task and its attempts are exhausted"
            await task_service.transition_task(
                db,
                task,
                TaskStatus.FAILED,
                message=task.error,
                payload={"worker": worker_id, "attempts": task.attempts},
            )
    await db.commit()


async def reap_expired(db: AsyncSession, *, limit: int = 50) -> int:
    """Return tasks whose lease lapsed to the queue. Returns how many moved.

    A task left ``running`` by a dead worker is the failure mode this exists for;
    the alternative (waiting for an operator to notice) is what makes a queue
    look stuck.
    """
    now = _now()
    stale = list(
        (
            await db.execute(
                select(Task)
                .where(
                    Task.status.in_((TaskStatus.RUNNING, TaskStatus.QUEUED)),
                    Task.lease_expires_at.is_not(None),
                    Task.lease_expires_at < now,
                )
                .order_by(Task.lease_expires_at.asc())
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).scalars()
    )
    for task in stale:
        owner = task.claimed_by
        task.claimed_by = None
        task.lease_expires_at = None
        task.attempts += 1
        if task.attempts >= task.max_attempts:
            task.error = "Worker stopped responding and attempts are exhausted"
            await task_service.transition_task(
                db,
                task,
                TaskStatus.FAILED,
                message=task.error,
                payload={"worker": owner, "attempts": task.attempts},
            )
        else:
            if task.status == TaskStatus.RUNNING:
                task.status = TaskStatus.QUEUED
            await task_service.add_event(
                db,
                task_id=task.id,
                event_type="task.lease_expired",
                message=f"Lease held by worker '{owner}' expired; task requeued",
                payload={"worker": owner, "attempt": task.attempts},
            )
        logger.warning("task_lease_reaped", task_id=str(task.id), worker=owner)
    if stale:
        await db.commit()
    return len(stale)


async def queue_depth(db: AsyncSession) -> int:
    from sqlalchemy import func

    return int(
        await db.scalar(
            select(func.count()).select_from(Task).where(Task.status.in_(CLAIMABLE_STATUSES))
        )
        or 0
    )
