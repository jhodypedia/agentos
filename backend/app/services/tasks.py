"""Task lifecycle: creation, listing and state transitions."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.exceptions import ConflictError, NotFoundError, UnprocessableError
from app.db.enums import TERMINAL_TASK_STATUSES, TaskStatus
from app.db.models import Agent, Task, TaskEvent, TaskStep

ALLOWED_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    # A task can fail before it ever runs: the worker parks it when its attempts
    # are exhausted, and a dependency that failed leaves its dependants with no
    # input. Allowing that edge is what keeps those tasks out of the queue
    # instead of silently sitting in it forever.
    TaskStatus.PENDING: frozenset(
        {TaskStatus.PLANNING, TaskStatus.QUEUED, TaskStatus.CANCELLED, TaskStatus.FAILED}
    ),
    TaskStatus.PLANNING: frozenset({TaskStatus.QUEUED, TaskStatus.FAILED, TaskStatus.CANCELLED}),
    TaskStatus.QUEUED: frozenset(
        {TaskStatus.RUNNING, TaskStatus.CANCELLED, TaskStatus.PAUSED, TaskStatus.FAILED}
    ),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.WAITING_APPROVAL,
            TaskStatus.PAUSED,
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.WAITING_APPROVAL: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED}),
    TaskStatus.PAUSED: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED}),
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}


async def list_tasks(
    db: AsyncSession,
    *,
    status: TaskStatus | None = None,
    agent_id: UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Task], int]:
    conditions = []
    if status is not None:
        conditions.append(Task.status == status)
    if agent_id is not None:
        conditions.append(Task.assigned_agent_id == agent_id)

    total_statement = select(func.count(Task.id))
    statement = select(Task).order_by(Task.created_at.desc()).limit(limit).offset(offset)
    if conditions:
        total_statement = total_statement.where(*conditions)
        statement = statement.where(*conditions)

    total = int((await db.execute(total_statement)).scalar_one())
    result = await db.execute(statement)
    return list(result.scalars()), total


async def get_task(db: AsyncSession, task_id: UUID) -> Task:
    statement = (
        select(Task)
        .where(Task.id == task_id)
        .options(selectinload(Task.steps), selectinload(Task.events))
    )
    result = await db.execute(statement)
    task = result.scalar_one_or_none()
    if task is None:
        raise NotFoundError("Task not found")
    return task


async def create_task(
    db: AsyncSession,
    *,
    title: str,
    objective: str,
    description: str = "",
    assigned_agent_id: UUID | None = None,
    created_by: UUID | None = None,
    project_id: UUID | None = None,
    input_payload: dict[str, Any] | None = None,
    budget_tokens: int | None = None,
    budget_usd: Any | None = None,
    max_iterations: int | None = None,
    timeout_seconds: int | None = None,
    deadline_at: datetime | None = None,
    status: TaskStatus = TaskStatus.PENDING,
    meta: dict[str, Any] | None = None,
    parent_task_id: UUID | None = None,
    priority: int = 100,
    max_attempts: int = 1,
) -> Task:
    if not title.strip():
        raise UnprocessableError("title must not be empty")
    if not objective.strip():
        raise UnprocessableError("objective must not be empty")

    if assigned_agent_id is not None:
        agent = await db.get(Agent, assigned_agent_id)
        if agent is None:
            raise UnprocessableError("The selected agent does not exist")

    if max_attempts < 1:
        raise UnprocessableError("max_attempts must be at least 1")

    if parent_task_id is not None:
        parent = await db.get(Task, parent_task_id)
        if parent is None:
            raise UnprocessableError("The parent task does not exist")
        if parent.parent_task_id is not None:
            raise UnprocessableError("A task may only be delegated one level deep")

    task = Task(
        title=title.strip(),
        objective=objective.strip(),
        description=description,
        assigned_agent_id=assigned_agent_id,
        created_by=created_by,
        project_id=project_id,
        parent_task_id=parent_task_id,
        priority=priority,
        max_attempts=max_attempts,
        input=input_payload or {},
        budget_tokens=budget_tokens,
        budget_usd=budget_usd,
        max_iterations=max_iterations if max_iterations is not None else 50,
        timeout_seconds=timeout_seconds if timeout_seconds is not None else 3600,
        deadline_at=deadline_at,
        status=status,
        meta=meta or {},
    )
    db.add(task)
    await db.flush()
    await add_event(
        db,
        task_id=task.id,
        event_type="task.created",
        message=f"Task created: {task.title}",
        payload={"status": task.status.value, "agent_id": str(assigned_agent_id)}
        if assigned_agent_id
        else {"status": task.status.value},
    )
    return task


async def transition_task(
    db: AsyncSession,
    task: Task,
    new_status: TaskStatus,
    *,
    message: str = "",
    payload: dict[str, Any] | None = None,
) -> Task:
    if task.status == new_status:
        return task
    if new_status not in ALLOWED_TRANSITIONS[task.status]:
        raise ConflictError(
            f"Cannot move a task from '{task.status.value}' to '{new_status.value}'"
        )

    task.status = new_status
    now = datetime.now(UTC)
    if new_status == TaskStatus.RUNNING and task.started_at is None:
        task.started_at = now
    if new_status in TERMINAL_TASK_STATUSES:
        task.completed_at = now
        # Drop the lease so the reaper never looks at a finished row, but keep
        # `claimed_by`: it is the record of which worker ran the task, and losing
        # it would make a completed task's provenance unknowable.
        task.lease_expires_at = None

    await add_event(
        db,
        task_id=task.id,
        event_type=f"task.{new_status.value}",
        message=message or f"Task moved to {new_status.value}",
        payload=payload or {},
    )
    await db.flush()
    return task


async def cancel_task(db: AsyncSession, task: Task, *, message: str = "") -> Task:
    """Cancel a task, refusing to silently re-cancel a finished one."""
    if task.status in TERMINAL_TASK_STATUSES:
        raise ConflictError(f"Task is already {task.status.value}")
    return await transition_task(
        db, task, TaskStatus.CANCELLED, message=message or "Task cancelled"
    )


async def add_event(
    db: AsyncSession,
    *,
    task_id: UUID,
    event_type: str,
    message: str = "",
    payload: dict[str, Any] | None = None,
    step_id: UUID | None = None,
    agent_id: UUID | None = None,
) -> TaskEvent:
    event = TaskEvent(
        task_id=task_id,
        step_id=step_id,
        agent_id=agent_id,
        event_type=event_type,
        message=message,
        payload=payload or {},
    )
    db.add(event)
    await db.flush()
    return event


async def add_step(
    db: AsyncSession,
    *,
    task_id: UUID,
    seq: int,
    kind: str,
    title: str = "",
    description: str = "",
    agent_id: UUID | None = None,
    input_payload: dict[str, Any] | None = None,
) -> TaskStep:
    from app.db.enums import StepKind, StepStatus

    step = TaskStep(
        task_id=task_id,
        seq=seq,
        kind=StepKind(kind),
        status=StepStatus.PENDING,
        title=title,
        description=description,
        agent_id=agent_id,
        input=input_payload or {},
    )
    db.add(step)
    await db.flush()
    return step


async def reopen_for_retry(
    db: AsyncSession,
    task: Task,
    *,
    message: str = "",
    payload: dict[str, Any] | None = None,
) -> Task:
    """Put a failed task back into the queue for another attempt.

    ``FAILED`` is terminal for operator-facing transitions — a failed task must
    never silently look retryable. The orchestrator, however, retries
    individual child tasks, so it needs an explicit, audited path back into
    ``QUEUED``. Only ``FAILED`` can be reopened; nothing else is a retry.
    """
    if task.status != TaskStatus.FAILED:
        raise ConflictError(f"Only a failed task can be retried, not a '{task.status.value}' one")
    if task.attempts >= task.max_attempts:
        raise ConflictError(
            f"Task reached its attempt limit ({task.max_attempts}); it cannot be retried"
        )

    task.status = TaskStatus.QUEUED
    task.attempts += 1
    task.completed_at = None
    task.error = None
    await add_event(
        db,
        task_id=task.id,
        event_type="task.retry",
        message=message or f"Retrying task (attempt {task.attempts} of {task.max_attempts})",
        payload=payload or {"attempt": task.attempts, "max_attempts": task.max_attempts},
    )
    await db.flush()
    return task


async def list_children(db: AsyncSession, task_id: UUID) -> list[Task]:
    """Direct children of a task, oldest first."""
    result = await db.execute(
        select(Task).where(Task.parent_task_id == task_id).order_by(Task.created_at.asc())
    )
    return list(result.scalars())
