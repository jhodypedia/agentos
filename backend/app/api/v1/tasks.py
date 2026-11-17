"""Task endpoints, including synchronous execution."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Query, Request, status

from app.api.deps import DbSession, OperatorPrincipal
from app.core.exceptions import UnprocessableError
from app.db.enums import TaskStatus
from app.schemas.task import (
    TaskCreate,
    TaskDetail,
    TaskPage,
    TaskPublic,
    TaskRunResponse,
)
from app.services import execution as execution_service
from app.services import tasks as task_service
from app.services.audit import record_audit
from app.tools.builtin import default_tool_registry

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get("", response_model=TaskPage, summary="List tasks")
async def list_tasks(
    principal: OperatorPrincipal,
    db: DbSession,
    task_status: TaskStatus | None = Query(default=None, alias="status"),
    agent_id: UUID | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> TaskPage:
    tasks, total = await task_service.list_tasks(
        db, status=task_status, agent_id=agent_id, limit=limit, offset=offset
    )
    return TaskPage(
        total=total,
        limit=limit,
        offset=offset,
        items=[TaskPublic.model_validate(task) for task in tasks],
    )


@router.post(
    "",
    response_model=TaskDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Create a task",
)
async def create_task(
    payload: TaskCreate,
    principal: OperatorPrincipal,
    db: DbSession,
    request: Request,
) -> TaskDetail:
    task = await task_service.create_task(
        db,
        title=payload.title,
        objective=payload.objective,
        description=payload.description,
        assigned_agent_id=payload.assigned_agent_id,
        created_by=principal.user.id,
        project_id=payload.project_id,
        input_payload=payload.input,
        budget_tokens=payload.budget_tokens,
        budget_usd=payload.budget_usd,
        max_iterations=payload.max_iterations,
        timeout_seconds=payload.timeout_seconds,
        deadline_at=payload.deadline_at,
        meta=payload.meta,
    )
    await record_audit(
        db,
        action="task.created",
        resource_type="task",
        resource_id=task.id,
        actor_user_id=principal.user.id,
        request=request,
        meta={"agent_id": str(payload.assigned_agent_id) if payload.assigned_agent_id else None},
    )
    await db.commit()
    task = await task_service.get_task(db, task.id)
    return TaskDetail.model_validate(task)


@router.get("/{task_id}", response_model=TaskDetail, summary="Get a task with its history")
async def get_task(task_id: UUID, principal: OperatorPrincipal, db: DbSession) -> TaskDetail:
    task = await task_service.get_task(db, task_id)
    return TaskDetail.model_validate(task)


@router.post(
    "/{task_id}/run",
    response_model=TaskRunResponse,
    summary="Execute a task synchronously with its assigned agent",
)
async def run_task(
    task_id: UUID,
    principal: OperatorPrincipal,
    db: DbSession,
    request: Request,
) -> TaskRunResponse:
    registry = default_tool_registry()
    bus = getattr(request.app.state, "event_bus", None)
    bind = db.bind
    if bind is None:
        raise UnprocessableError("The database session factory is unavailable")

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        bind=bind, expire_on_commit=False, autoflush=False
    )

    result = await execution_service.execute_task(
        db,
        task_id=task_id,
        tools=registry,
        factory=session_factory,
        bus=bus,
    )
    task = await task_service.get_task(db, task_id)
    await record_audit(
        db,
        action="task.executed",
        resource_type="task",
        resource_id=task.id,
        actor_user_id=principal.user.id,
        request=request,
        success=result.ok,
        meta={
            "iterations": result.iterations,
            "tokens": result.usage.total_tokens,
            "cost_usd": str(result.usage.cost_usd),
        },
    )
    await db.commit()

    return TaskRunResponse(
        task_id=task.id,
        status=task.status,
        content=result.content,
        iterations=result.iterations,
        total_tokens=result.usage.total_tokens,
        cost_usd=result.usage.cost_usd,
        error=result.error,
        tool_invocations=result.tool_log(),
        pending_approvals=result.pending_approvals,
    )


@router.post(
    "/{task_id}/cancel",
    response_model=TaskPublic,
    summary="Cancel a task that has not finished",
)
async def cancel_task(
    task_id: UUID,
    principal: OperatorPrincipal,
    db: DbSession,
    request: Request,
) -> TaskPublic:
    task = await task_service.get_task(db, task_id)
    task = await task_service.cancel_task(db, task, message="Task cancelled by operator")
    await record_audit(
        db,
        action="task.cancelled",
        resource_type="task",
        resource_id=task.id,
        actor_user_id=principal.user.id,
        request=request,
    )
    await db.commit()
    await db.refresh(task)
    return TaskPublic.model_validate(task)


@router.get(
    "/{task_id}/events",
    summary="Task event log",
)
async def task_events(
    task_id: UUID, principal: OperatorPrincipal, db: DbSession
) -> list[dict[str, object]]:
    task = await task_service.get_task(db, task_id)
    return [
        {
            "id": str(event.id),
            "type": event.event_type,
            "level": event.level.value,
            "message": event.message,
            "payload": event.payload,
            "created_at": event.created_at.isoformat(),
        }
        for event in sorted(task.events, key=lambda item: item.created_at)
    ]
