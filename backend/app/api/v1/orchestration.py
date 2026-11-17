"""Orchestration endpoints: plan preview and multi-agent execution."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import DbSession, OperatorPrincipal
from app.core.exceptions import NotFoundError, UnprocessableError
from app.db.enums import AgentStatus
from app.orchestrator.plan import ExecutionPlan, PlanStep
from app.orchestrator.planner import LlmPlanner, single_step_plan
from app.orchestrator.specialists import ORCHESTRATOR_SLUG, specialist_catalogue
from app.providers.registry import provider_from_record
from app.schemas.orchestration import (
    OrchestrateRequest,
    OrchestrationOptionsRequest,
    OrchestrationResult,
    PlanPublic,
    PlanRequest,
    PlanStepPublic,
    StepResultPublic,
)
from app.services import orchestration as orchestration_service
from app.services import tasks as task_service
from app.services.agents import get_agent_by_slug
from app.services.audit import record_audit
from app.tools.builtin import default_tool_registry

router = APIRouter(prefix="/orchestration", tags=["orchestration"])


def _session_factory(db: AsyncSession) -> async_sessionmaker[AsyncSession]:
    bind = db.bind
    if bind is None:
        raise UnprocessableError("The database session factory is unavailable")
    return async_sessionmaker(bind=bind, expire_on_commit=False, autoflush=False)


def _plan_payload(plan: ExecutionPlan) -> PlanPublic:
    return PlanPublic(
        objective=plan.objective,
        rationale=plan.rationale,
        steps=[
            PlanStepPublic(
                key=step.key,
                title=step.title,
                instruction=step.instruction,
                agent_slug=step.agent_slug,
                mode=step.mode,
                depends_on=list(step.depends_on),
                priority=step.priority,
            )
            for step in plan.steps
        ],
    )


def _plan_from_payload(payload: PlanPublic) -> ExecutionPlan:
    return ExecutionPlan(
        objective=payload.objective,
        rationale=payload.rationale,
        steps=[
            PlanStep(
                key=step.key,
                title=step.title,
                instruction=step.instruction,
                agent_slug=step.agent_slug,
                mode=step.mode,
                depends_on=tuple(step.depends_on),
                priority=step.priority,
            )
            for step in payload.steps
        ],
    )


def _options(payload: OrchestrationOptionsRequest) -> orchestration_service.OrchestrationOptions:
    return orchestration_service.OrchestrationOptions(
        max_parallel=payload.max_parallel,
        max_step_attempts=payload.max_step_attempts,
        use_llm_planner=payload.use_llm_planner,
        verify=payload.verify,
        verifier_slug=payload.verifier_slug,
    )


@router.post("/plan", response_model=PlanPublic, summary="Preview a plan for an objective")
async def plan_objective(
    payload: PlanRequest, principal: OperatorPrincipal, db: DbSession
) -> PlanPublic:
    orchestrator = await get_agent_by_slug(db, ORCHESTRATOR_SLUG)
    if orchestrator is None:
        raise NotFoundError("The orchestrator agent is not seeded; run 'agentos seed-agents'")
    if orchestrator.status != AgentStatus.ACTIVE:
        raise UnprocessableError("The orchestrator agent is not active")

    catalogue = await specialist_catalogue(db)
    if orchestrator.model_provider_id is None or not orchestrator.model_name:
        return _plan_payload(
            single_step_plan(objective=payload.objective, agent_slug=orchestrator.slug)
        )

    from app.db.models import ModelProvider

    record = await db.get(ModelProvider, orchestrator.model_provider_id)
    if record is None:
        raise UnprocessableError("The orchestrator's model provider no longer exists")

    provider = provider_from_record(record)
    planner = LlmPlanner(
        provider=provider,
        model=orchestrator.model_name,
        agent_slug=orchestrator.slug,
        available_agents=catalogue,
    )
    try:
        plan = await planner.plan(objective=payload.objective, context=payload.context)
    finally:
        close = getattr(provider, "aclose", None)
        if close is not None:
            await close()

    if len(plan.steps) > payload.max_steps:
        plan.steps = plan.steps[: payload.max_steps]
        plan.validate()

    return _plan_payload(plan)


@router.post(
    "/tasks/{task_id}/run",
    response_model=OrchestrationResult,
    summary="Run a task as a multi-agent orchestration",
)
async def orchestrate_task(
    task_id: UUID,
    payload: OrchestrateRequest,
    principal: OperatorPrincipal,
    db: DbSession,
    request: Request,
) -> OrchestrationResult:
    task = await task_service.get_task(db, task_id)
    plan = _plan_from_payload(payload.plan) if payload.plan is not None else None
    if plan is not None:
        plan.validate()

    registry = default_tool_registry()
    bus = getattr(request.app.state, "event_bus", None)

    outcome = await orchestration_service.orchestrate_task(
        db,
        task_id=task.id,
        factory=_session_factory(db),
        tools=registry,
        bus=bus,
        options=_options(payload),
        plan_override=plan,
    )
    await record_audit(
        db,
        action="task.orchestrated",
        resource_type="task",
        resource_id=task.id,
        actor_user_id=principal.user.id,
        request=request,
        success=outcome.error is None,
        meta={
            "steps": len(outcome.plan.steps) if outcome.plan else 0,
            "status": outcome.status.value,
            "tokens": outcome.totals.get("tokens", 0),
        },
    )
    await db.commit()

    await db.refresh(task)
    stored = task.output or {}
    return OrchestrationResult(
        task_id=str(task.id),
        status=task.status.value,
        content=str(stored.get("content", outcome.content)),
        error=task.error,
        plan=_plan_payload(outcome.plan) if outcome.plan else None,
        steps=[StepResultPublic(**step) for step in stored.get("steps", [])],
        children={key: str(value) for key, value in outcome.children.items()},
        totals=outcome.totals,
        summary=stored.get("summary", {}),
        needs_approval=outcome.needs_approval,
    )


@router.post(
    "/tasks/{task_id}/resume",
    response_model=OrchestrationResult,
    summary="Resume a planned orchestration after an approval",
)
async def resume_task(
    task_id: UUID,
    payload: OrchestrationOptionsRequest,
    principal: OperatorPrincipal,
    db: DbSession,
    request: Request,
) -> OrchestrationResult:
    registry = default_tool_registry()
    bus = getattr(request.app.state, "event_bus", None)
    outcome = await orchestration_service.resume_orchestration(
        db,
        task_id=task_id,
        factory=_session_factory(db),
        tools=registry,
        bus=bus,
        options=_options(payload),
    )
    await record_audit(
        db,
        action="task.resumed",
        resource_type="task",
        resource_id=task_id,
        actor_user_id=principal.user.id,
        request=request,
        success=outcome.error is None,
        meta={"status": outcome.status.value},
    )
    await db.commit()

    task = await task_service.get_task(db, task_id)
    stored = task.output or {}
    return OrchestrationResult(
        task_id=str(task.id),
        status=task.status.value,
        content=str(stored.get("content", outcome.content)),
        error=task.error,
        plan=_plan_payload(outcome.plan) if outcome.plan else None,
        steps=[StepResultPublic(**step) for step in stored.get("steps", [])],
        children={key: str(value) for key, value in outcome.children.items()},
        totals=outcome.totals,
        summary=stored.get("summary", {}),
        needs_approval=outcome.needs_approval,
    )


@router.get(
    "/tasks/{task_id}/children",
    summary="Child tasks delegated by an orchestrated task",
)
async def task_children(
    task_id: UUID, principal: OperatorPrincipal, db: DbSession
) -> list[dict[str, Any]]:
    children = await task_service.list_children(db, task_id)
    return [
        {
            "id": str(child.id),
            "title": child.title,
            "plan_key": (child.meta or {}).get("plan_key"),
            "status": child.status.value,
            "agent_id": str(child.assigned_agent_id) if child.assigned_agent_id else None,
            "attempts": child.attempts,
            "max_attempts": child.max_attempts,
            "tokens": child.used_tokens,
            "cost_usd": str(child.cost_usd),
            "error": child.error,
        }
        for child in children
    ]


@router.get(
    "/tasks/{task_id}/messages",
    summary="Agent-to-agent conversation for a task",
)
async def task_messages(
    task_id: UUID, principal: OperatorPrincipal, db: DbSession
) -> list[dict[str, Any]]:
    from app.services.messages import transcript

    await task_service.get_task(db, task_id)
    return await transcript(db, task_id=task_id)
