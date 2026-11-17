"""Orchestration service: plan, delegate, verify and resume multi-agent tasks.

Lifecycle of an orchestrated task:

1. ``PENDING`` → ``PLANNING``: build a plan (LLM planner with a deterministic
   fallback) and persist it as mirrored ``task_steps`` rows.
2. Each plan step becomes a **child task** (``parent_task_id``) assigned to the
   specialist agent, with ``task_dependencies`` edges mirroring the plan's DAG.
3. ``PLANNING`` → ``QUEUED`` → ``RUNNING``: the scheduler executes the DAG level
   by level; each level runs its steps in parallel with a concurrency cap.
4. A child that fails is retried by reopening it (audited) up to its attempt
   limit; if it still fails, its dependents are skipped and the parent fails.
5. A step that needs human approval parks the parent in ``WAITING_APPROVAL``
   with an ``Approval`` row; the resume endpoint continues from the plan.
6. The parent aggregates child outputs, optionally runs a verification pass,
   writes the final output and settles on ``COMPLETED``/``FAILED``.

Every child task keeps its own token/cost accounting, so the parent's totals are
a real sum rather than an estimate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.events import RedisEventBus
from app.core.exceptions import ConflictError, UnprocessableError
from app.db.enums import (
    TERMINAL_TASK_STATUSES,
    ApprovalStatus,
    StepKind,
    StepStatus,
    TaskDependencyType,
    TaskStatus,
)
from app.db.models import Agent, Approval, ModelProvider, Task, TaskDependency, TaskStep
from app.orchestrator.plan import ExecutionPlan, PlanStep
from app.orchestrator.planner import LlmPlanner, Planner, render_dependency_context
from app.orchestrator.scheduler import ScheduleResult, StepOutcome, run_plan
from app.orchestrator.specialists import ORCHESTRATOR_SLUG, specialist_catalogue
from app.providers.registry import provider_from_record
from app.runtime.types import RuntimeResult
from app.services import execution as execution_service
from app.services import tasks as task_service
from app.services.agents import get_agent_by_slug
from app.services.messages import record_message
from app.tools.base import ToolRegistry

logger = structlog.get_logger("agentos.services.orchestration")

MAX_PARALLEL_DEFAULT = 3
MAX_STEP_ATTEMPTS = 2
MAX_PLAN_STEPS = 12


@dataclass(slots=True)
class OrchestrationOptions:
    max_parallel: int = MAX_PARALLEL_DEFAULT
    max_step_attempts: int = MAX_STEP_ATTEMPTS
    use_llm_planner: bool = True
    verify: bool = False
    verifier_slug: str = "independent-reviewer"
    retry_delay: float = 0.0


@dataclass(slots=True)
class OrchestrationOutcome:
    task_id: UUID
    status: TaskStatus
    plan: ExecutionPlan | None = None
    schedule: ScheduleResult | None = None
    children: dict[str, UUID] = field(default_factory=dict)
    content: str = ""
    error: str | None = None
    totals: dict[str, Any] = field(default_factory=dict)
    needs_approval: bool = False


# --------------------------------------------------------------------- planning
async def _resolve_planner(
    db: AsyncSession,
    *,
    orchestrator: Agent,
    options: OrchestrationOptions,
    parent: Task,
) -> Planner:
    """Build an LLM planner for the orchestrator agent, or a static fallback."""
    catalogue = await specialist_catalogue(db)
    fallback_slug = ORCHESTRATOR_SLUG if ORCHESTRATOR_SLUG in catalogue else orchestrator.slug

    if not options.use_llm_planner or orchestrator.model_provider_id is None:
        from app.orchestrator.planner import single_step_plan

        objective = parent.objective
        return _SingleStepPlanner(
            single_step_plan(
                objective=objective,
                agent_slug=fallback_slug,
                rationale="Single-step plan (no planner model configured).",
            )
        )

    provider_record = await db.get(ModelProvider, orchestrator.model_provider_id)
    if provider_record is None or not orchestrator.model_name:
        from app.orchestrator.planner import single_step_plan

        return _SingleStepPlanner(
            single_step_plan(
                objective=parent.objective,
                agent_slug=fallback_slug,
                rationale="Single-step plan (the orchestrator has no usable model).",
            )
        )

    provider = provider_from_record(provider_record)
    return LlmPlanner(
        provider=provider,
        model=orchestrator.model_name,
        agent_slug=fallback_slug,
        available_agents=catalogue,
    )


@dataclass(slots=True)
class _SingleStepPlanner:
    plan_result: ExecutionPlan

    async def plan(self, *, objective: str, context: str | None = None) -> ExecutionPlan:
        return self.plan_result


async def _persist_plan(
    db: AsyncSession, *, task: Task, plan: ExecutionPlan, agents_by_slug: dict[str, Agent]
) -> dict[str, TaskStep]:
    """Mirror the plan into parent ``task_steps`` rows, keyed by plan step key."""
    existing = {
        str((step.input or {}).get("plan_key")): step
        for step in (
            await db.execute(select(TaskStep).where(TaskStep.task_id == task.id))
        ).scalars()
        if (step.input or {}).get("plan_key")
    }

    steps: dict[str, TaskStep] = {}
    for index, plan_step in enumerate(plan.steps, start=1):
        agent = agents_by_slug.get(plan_step.agent_slug or "")
        key = plan_step.key
        record = existing.get(key)
        if record is None:
            record = TaskStep(
                task_id=task.id,
                seq=index,
                kind=StepKind.AGENT if plan_step.mode == "agent" else StepKind.PLAN,
                status=StepStatus.PENDING,
                title=plan_step.title,
                description=plan_step.instruction,
                agent_id=agent.id if agent else None,
                depends_on=list(plan_step.depends_on),
                max_attempts=plan_step.metadata.get("max_attempts", 1)
                if isinstance(plan_step.metadata, dict)
                else 1,
                input={"plan_key": key, "agent_slug": plan_step.agent_slug},
            )
            db.add(record)
        else:
            record.seq = index
            record.title = plan_step.title
            record.description = plan_step.instruction
            record.depends_on = list(plan_step.depends_on)
            record.agent_id = agent.id if agent else None
            record.input = {**(record.input or {}), "plan_key": key}
        steps[key] = record

    await db.flush()
    return steps


async def _agents_for_plan(db: AsyncSession, plan: ExecutionPlan) -> dict[str, Agent]:
    slugs = {step.agent_slug for step in plan.steps if step.agent_slug}
    agents: dict[str, Agent] = {}
    for slug in slugs:
        agent = await get_agent_by_slug(db, slug)
        if agent is None:
            raise UnprocessableError(
                "The plan references an agent that does not exist", details={"slug": slug}
            )
        agents[slug] = agent
    return agents


# -------------------------------------------------------------------- delegation
async def _create_children(
    db: AsyncSession,
    *,
    parent: Task,
    plan: ExecutionPlan,
    agents_by_slug: dict[str, Agent],
    max_attempts: int = 1,
    existing: dict[str, UUID] | None = None,
) -> dict[str, UUID]:
    """Create (or reuse) one child task per plan step, plus dependency edges.

    ``existing`` maps plan keys to child task ids from a previous run, so
    resuming a task never duplicates its delegation tree.
    """
    existing = existing or {}
    children: dict[str, UUID] = {}
    key_to_task: dict[str, UUID] = {}

    for plan_step in plan.steps:
        agent = agents_by_slug.get(plan_step.agent_slug or "")
        if agent is None:
            raise UnprocessableError(f"Unknown agent for step '{plan_step.key}'")

        child_id = existing.get(plan_step.key)
        child = await db.get(Task, child_id) if child_id else None
        if child is None:
            child = await task_service.create_task(
                db,
                title=f"{parent.title} / {plan_step.title}"[:300],
                objective=plan_step.instruction,
                description=parent.description,
                assigned_agent_id=agent.id,
                created_by=parent.created_by,
                project_id=parent.project_id,
                input_payload={
                    "plan_key": plan_step.key,
                    "parent_objective": parent.objective,
                    "depends_on": list(plan_step.depends_on),
                },
                parent_task_id=parent.id,
                priority=plan_step.priority,
                max_attempts=max_attempts,
                timeout_seconds=plan_step.timeout_seconds or parent.timeout_seconds,
                status=TaskStatus.PENDING,
                meta={"orchestrated": True, "plan_key": plan_step.key},
            )
        children[plan_step.key] = child.id
        key_to_task[plan_step.key] = child.id

    for plan_step in plan.steps:
        child_id = key_to_task[plan_step.key]
        for dependency in plan_step.depends_on:
            dependency_id = key_to_task.get(dependency)
            if dependency_id is None:
                continue
            exists = await db.execute(
                select(TaskDependency.id).where(
                    TaskDependency.task_id == child_id,
                    TaskDependency.depends_on_task_id == dependency_id,
                )
            )
            if exists.scalar_one_or_none() is not None:
                continue
            db.add(
                TaskDependency(
                    task_id=child_id,
                    depends_on_task_id=dependency_id,
                    dependency_type=TaskDependencyType.FINISH_TO_START,
                )
            )

    await db.flush()
    return children


# --------------------------------------------------------------------- execution
class _ChildStepRunner:
    """Execute one plan step as its child task, with its own DB session."""

    def __init__(
        self,
        *,
        factory: async_sessionmaker[AsyncSession],
        parent_id: UUID,
        parent_task: Task,
        plan: ExecutionPlan,
        children: dict[str, UUID],
        steps: dict[str, TaskStep],
        tools: ToolRegistry,
        bus: RedisEventBus | None,
        orchestrator_agent: Agent,
        orchestrator_provider: Any | None,
        cancel_check: Any,
        options: OrchestrationOptions,
    ) -> None:
        self._factory = factory
        self._parent_id = parent_id
        self._parent_task = parent_task
        self._plan = plan
        self._children = children
        self._steps = steps
        self._tools = tools
        self._bus = bus
        self._orchestrator_agent = orchestrator_agent
        self._orchestrator_provider = orchestrator_provider
        self._cancel_check = cancel_check
        self._options = options
        self.outputs: dict[str, str] = {}
        self.results: dict[str, RuntimeResult] = {}

    async def _mark_step(
        self, session: AsyncSession, key: str, status: StepStatus, **fields: Any
    ) -> None:
        step = await session.get(TaskStep, self._steps[key].id)
        if step is None:
            return
        step.status = status
        if status == StepStatus.RUNNING and step.started_at is None:
            step.started_at = datetime.now(UTC)
        if status in {
            StepStatus.COMPLETED,
            StepStatus.FAILED,
            StepStatus.SKIPPED,
            StepStatus.CANCELLED,
        }:
            step.completed_at = datetime.now(UTC)
        for name, value in fields.items():
            setattr(step, name, value)

    async def __call__(self, step: PlanStep, attempt: int) -> StepOutcome:
        key = step.key
        dependency_context = render_dependency_context(step, self.outputs)
        child_id = self._children[key]

        async with self._factory() as session:
            await self._mark_step(session, key, StepStatus.RUNNING, attempt=attempt - 1)
            await session.commit()

        try:
            provider = self._orchestrator_provider if step.mode == "agent" else None
            model_name = self._orchestrator_agent.model_name if step.mode == "agent" else None

            async with self._factory() as session:
                child = await task_service.get_task(session, child_id)
                if child.status == TaskStatus.COMPLETED:
                    content = str((child.output or {}).get("content", ""))
                    self.outputs[key] = content
                    await self._mark_step(session, key, StepStatus.COMPLETED)
                    await session.commit()
                    return StepOutcome(
                        ok=True,
                        output=content,
                        attempts=child.attempts or 1,
                        tokens=child.used_tokens,
                        cost_usd=str(child.cost_usd),
                    )
                if child.status == TaskStatus.FAILED:
                    await task_service.reopen_for_retry(
                        session,
                        child,
                        message=f"Retrying step '{key}' (attempt {attempt})",
                    )
                result = await execution_service.execute_task(
                    session,
                    task_id=child_id,
                    tools=self._tools,
                    factory=self._factory,
                    bus=self._bus,
                    cancel_check=self._cancel_check,
                    extra_context=dependency_context or None,
                    model_name=model_name,
                    provider_override=provider,
                )

            self.results[key] = result
            if result.ok:
                self.outputs[key] = result.content
                async with self._factory() as session:
                    await self._mark_step(
                        session,
                        key,
                        StepStatus.COMPLETED,
                        output={"content": result.content[:4_000]},
                        error=None,
                    )
                    await self._record_handoff(session, key, result)
                    await session.commit()
                return StepOutcome(
                    ok=True,
                    output=result.content,
                    attempts=attempt,
                    tokens=result.usage.total_tokens,
                    cost_usd=str(result.usage.cost_usd),
                )

            async with self._factory() as session:
                await self._mark_step(
                    session, key, StepStatus.FAILED, error=(result.error or "")[:2_000]
                )
                await session.commit()
            return StepOutcome(
                ok=False,
                error=result.error or "The step failed",
                attempts=attempt,
                tokens=result.usage.total_tokens,
                cost_usd=str(result.usage.cost_usd),
                metadata={"retryable": attempt < self._options.max_step_attempts},
            )
        except Exception as exc:
            logger.exception("step_execution_failed", step=key, attempt=attempt)
            async with self._factory() as session:
                await self._mark_step(session, key, StepStatus.FAILED, error=str(exc)[:2_000])
                await session.commit()
            return StepOutcome(
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                attempts=attempt,
                metadata={"retryable": attempt < self._options.max_step_attempts},
            )

    async def _record_handoff(self, session: AsyncSession, key: str, result: RuntimeResult) -> None:
        """Persist the specialist's report as an agent-to-agent message."""
        child_id = self._children.get(key)
        if child_id is None:
            return
        child = await session.get(Task, child_id)
        if child is None or child.assigned_agent_id is None:
            return
        await record_message(
            session,
            task_id=self._parent_id,
            from_agent_id=child.assigned_agent_id,
            to_agent_id=self._orchestrator_agent.id,
            message_type="handoff",
            content=result.content[:20_000],
            payload={
                "plan_key": key,
                "child_task_id": str(child_id),
                "tokens": result.usage.total_tokens,
                "cost_usd": str(result.usage.cost_usd),
            },
        )

    async def mark_skipped(self, key: str) -> None:
        """Mark a step skipped and cancel its child so no task is left dangling."""
        async with self._factory() as session:
            await self._mark_step(session, key, StepStatus.SKIPPED, error="Dependency failed")
            child_id = self._children.get(key)
            if child_id is not None:
                child = await session.get(Task, child_id)
                if child is not None and child.status not in TERMINAL_TASK_STATUSES:
                    await task_service.cancel_task(
                        session,
                        child,
                        message="Skipped because a dependency failed",
                    )
            await session.commit()


# ------------------------------------------------------------------ orchestration
async def _load_orchestrator(db: AsyncSession, task: Task) -> Agent:
    agent = await db.get(Agent, task.assigned_agent_id) if task.assigned_agent_id else None
    if agent is None:
        agent = await get_agent_by_slug(db, ORCHESTRATOR_SLUG)
    if agent is None:
        raise UnprocessableError(
            "No orchestrator agent is available; seed the specialist agents first"
        )
    return agent


def _child_task_ids(schedule: ScheduleResult, children: dict[str, UUID]) -> list[UUID]:
    return [children[key] for key in schedule.outcomes if key in children]


async def _totals(db: AsyncSession, children: dict[str, UUID]) -> dict[str, Any]:
    """Aggregate real usage from child tasks rather than estimating it."""
    if not children:
        return {"tokens": 0, "cost_usd": "0", "iterations": 0, "children": {}}

    result = await db.execute(select(Task).where(Task.id.in_(list(children.values()))))
    tasks = {task.id: task for task in result.scalars()}
    tokens = sum(task.used_tokens for task in tasks.values())
    cost = sum((Decimal(task.cost_usd) for task in tasks.values()), Decimal("0"))
    iterations = sum(task.used_iterations for task in tasks.values())
    return {
        "tokens": tokens,
        "cost_usd": str(cost),
        "iterations": iterations,
        "children": {
            key: {
                "task_id": str(task.id),
                "status": task.status.value,
                "tokens": task.used_tokens,
                "cost_usd": str(task.cost_usd),
                "attempts": task.attempts,
            }
            for key, task_id in children.items()
            if (task := tasks.get(task_id)) is not None
        },
    }


def _compose_report(plan: ExecutionPlan, schedule: ScheduleResult) -> str:
    """Assemble the final content from successful step outputs, in plan order."""
    summary_ok = sum(1 for outcome in schedule.outcomes.values() if outcome.ok)
    blocks = [
        f"# {plan.objective.strip()}",
        "",
    ]
    if plan.rationale:
        blocks.extend([plan.rationale.strip(), ""])

    for step in plan.steps:
        outcome = schedule.outcomes.get(step.key)
        if step.key in schedule.skipped:
            blocks.extend([f"## {step.title}", "_Skipped: a dependency failed._", ""])
            continue
        if outcome is None:
            continue
        if not outcome.ok:
            blocks.extend([f"## {step.title}", f"_Failed: {outcome.error or 'unknown error'}_", ""])
            continue
        content = (outcome.output or "").strip()
        blocks.extend([f"## {step.title}", content or "_No output produced._", ""])

    blocks.extend(
        [
            "---",
            (
                f"Steps: {summary_ok} completed, "
                f"{len(schedule.failed)} failed, "
                f"{len(schedule.skipped)} skipped."
            ),
        ]
    )
    return "\n".join(blocks).strip()


async def _run_verification(
    db: AsyncSession,
    *,
    parent: Task,
    plan: ExecutionPlan,
    report: str,
    factory: async_sessionmaker[AsyncSession],
    tools: ToolRegistry,
    bus: RedisEventBus | None,
    orchestrator: Agent,
    provider: Any | None,
    cancel_check: Any,
) -> tuple[bool, str]:
    """Ask the reviewer agent to challenge the combined report."""
    reviewer = await get_agent_by_slug(db, "independent-reviewer")
    if reviewer is None or reviewer.status.value != "active":
        return True, ""

    review_task = await task_service.create_task(
        db,
        title=f"{parent.title} / verification"[:300],
        objective=(
            "Review the combined output below as an adversarial reviewer. Report only "
            "concrete objections with the change that resolves each one.\n\n" + report
        ),
        description=parent.description,
        assigned_agent_id=reviewer.id,
        created_by=parent.created_by,
        project_id=parent.project_id,
        parent_task_id=parent.id,
        priority=plan.steps[0].priority if plan.steps else 100,
        status=TaskStatus.PENDING,
        meta={"orchestrated": True, "plan_key": "__verification__"},
    )
    await db.commit()

    result = await execution_service.execute_task(
        db,
        task_id=review_task.id,
        tools=tools,
        factory=factory,
        bus=bus,
        cancel_check=cancel_check,
        model_name=orchestrator.model_name,
        provider_override=provider,
    )
    return result.ok, result.content


async def orchestrate_task(
    db: AsyncSession,
    *,
    task_id: UUID,
    factory: async_sessionmaker[AsyncSession],
    tools: ToolRegistry,
    bus: RedisEventBus | None = None,
    cancel_check: Any = None,
    options: OrchestrationOptions | None = None,
    plan_override: ExecutionPlan | None = None,
) -> OrchestrationOutcome:
    """Run a task as a multi-agent orchestration."""
    options = options or OrchestrationOptions()
    task = await task_service.get_task(db, task_id)

    if task.status in TERMINAL_TASK_STATUSES:
        raise ConflictError(f"Task is already {task.status.value}")
    if task.status in {TaskStatus.RUNNING, TaskStatus.WAITING_APPROVAL}:
        raise ConflictError(f"Task is already {task.status.value}")

    orchestrator = await _load_orchestrator(db, task)
    if task.assigned_agent_id is None:
        task.assigned_agent_id = orchestrator.id

    if task.status == TaskStatus.PENDING:
        await task_service.transition_task(
            db, task, TaskStatus.PLANNING, message="Building the execution plan"
        )
        await db.commit()

    plan = plan_override
    if plan is None:
        planner = await _resolve_planner(
            db, orchestrator=orchestrator, options=options, parent=task
        )
        try:
            plan = await planner.plan(objective=task.objective, context=task.description or None)
        except Exception as exc:
            logger.exception("planning_failed", task_id=str(task.id))
            await task_service.transition_task(
                db, task, TaskStatus.FAILED, message=f"Planning failed: {exc}"
            )
            await db.commit()
            return OrchestrationOutcome(
                task_id=task.id, status=TaskStatus.FAILED, error=f"Planning failed: {exc}"
            )

    if len(plan.steps) > MAX_PLAN_STEPS:
        raise UnprocessableError(f"A plan may contain at most {MAX_PLAN_STEPS} steps")

    agents_by_slug = await _agents_for_plan(db, plan)
    known = set(agents_by_slug)
    plan.validate(known_agents=known | {orchestrator.slug})

    steps = await _persist_plan(db, task=task, plan=plan, agents_by_slug=agents_by_slug)
    previous_children = {
        key: UUID(value)
        for key, value in ((task.output or {}).get("children") or {}).items()
        if isinstance(value, str)
    }
    children = await _create_children(
        db,
        parent=task,
        plan=plan,
        agents_by_slug=agents_by_slug,
        max_attempts=max(1, options.max_step_attempts),
        existing=previous_children,
    )
    await task_service.add_event(
        db,
        task_id=task.id,
        event_type="task.planned",
        message=f"Plan created with {len(plan.steps)} step(s)",
        payload={"plan": plan.to_dict(), "children": {k: str(v) for k, v in children.items()}},
    )
    task.output = {
        "plan": plan.to_dict(),
        "children": {key: str(value) for key, value in children.items()},
    }
    await db.commit()

    orchestrator_provider = None
    if orchestrator.model_provider_id is not None:
        record = await db.get(ModelProvider, orchestrator.model_provider_id)
        if record is not None:
            orchestrator_provider = provider_from_record(record)

    runner = _ChildStepRunner(
        factory=factory,
        parent_id=task.id,
        parent_task=task,
        plan=plan,
        children=children,
        steps=steps,
        tools=tools,
        bus=bus,
        orchestrator_agent=orchestrator,
        orchestrator_provider=orchestrator_provider,
        cancel_check=cancel_check,
        options=options,
    )

    await task_service.transition_task(
        db,
        task,
        TaskStatus.QUEUED,
        message=f"{len(plan.steps)} step(s) queued for execution",
        payload={"steps": [step.key for step in plan.steps]},
    )
    await task_service.transition_task(
        db,
        task,
        TaskStatus.RUNNING,
        message=f"Orchestration started by '{orchestrator.slug}'",
        payload={"agent_id": str(orchestrator.id), "model": orchestrator.model_name},
    )
    await db.commit()

    try:
        schedule = await run_plan(
            plan,
            runner,
            max_parallel=max(1, options.max_parallel),
            max_attempts=max(1, options.max_step_attempts),
            timeout_seconds=float(task.timeout_seconds) if task.timeout_seconds else None,
            cancel_check=cancel_check,
            retry_delay=options.retry_delay,
        )
    finally:
        close = getattr(orchestrator_provider, "aclose", None)
        if close is not None:
            await close()

    for skipped_key in schedule.skipped:
        await runner.mark_skipped(skipped_key)

    totals = await _totals(db, children)
    report = _compose_report(plan, schedule)

    if schedule.paused:
        # The step runner never paused a step in this phase; the branch exists so
        # that a future approval gate can park the task without changing callers.
        await db.refresh(task)
        await task_service.transition_task(
            db,
            task,
            TaskStatus.WAITING_APPROVAL,
            message=f"Waiting for approval at step '{schedule.paused_step}'",
            payload={"step": schedule.paused_step},
        )
        await db.commit()
        return OrchestrationOutcome(
            task_id=task.id,
            status=task.status,
            plan=plan,
            schedule=schedule,
            children=children,
            content=report,
            totals=totals,
            needs_approval=True,
        )

    verified = True
    review = ""
    if options.verify and schedule.ok:
        try:
            verified, review = await _run_verification(
                db,
                parent=task,
                plan=plan,
                report=report,
                factory=factory,
                tools=tools,
                bus=bus,
                orchestrator=orchestrator,
                provider=None,
                cancel_check=cancel_check,
            )
        except Exception:
            logger.exception("verification_failed", task_id=str(task.id))
            verified, review = False, ""

    await db.refresh(task)
    task.used_tokens = task.used_tokens + int(totals["tokens"])
    task.cost_usd = Decimal(task.cost_usd) + Decimal(totals["cost_usd"])
    task.used_iterations = task.used_iterations + int(totals["iterations"])
    task.output = {
        "content": report,
        "plan": plan.to_dict(),
        "plan_rationale": plan.rationale,
        "steps": aggregate_steps(schedule, plan),
        "summary": schedule.summary(),
        "children": totals["children"],
        "verification": {"performed": bool(options.verify), "passed": verified, "review": review},
    }

    if schedule.cancelled:
        await task_service.transition_task(
            db, task, TaskStatus.CANCELLED, message="Orchestration cancelled"
        )
    elif schedule.timed_out:
        task.error = "The orchestration exceeded its timeout"
        await task_service.transition_task(
            db, task, TaskStatus.FAILED, message="Orchestration timed out"
        )
    elif schedule.failed:
        task.error = "These steps failed: " + ", ".join(sorted(schedule.failed))
        await task_service.transition_task(
            db,
            task,
            TaskStatus.FAILED,
            message=task.error,
            payload={"failed_steps": sorted(schedule.failed)},
        )
    elif options.verify and not verified:
        await task_service.transition_task(
            db,
            task,
            TaskStatus.COMPLETED,
            message="Orchestration completed; the reviewer raised objections",
            payload={"verification": "objections"},
        )
    else:
        await task_service.transition_task(
            db,
            task,
            TaskStatus.COMPLETED,
            message=f"Orchestration completed with {len(plan.steps)} step(s)",
            payload=schedule.summary(),
        )

    await db.commit()
    await db.refresh(task)

    return OrchestrationOutcome(
        task_id=task.id,
        status=task.status,
        plan=plan,
        schedule=schedule,
        children=children,
        content=report,
        error=task.error,
        totals=totals,
    )


def aggregate_steps(schedule: ScheduleResult, plan: ExecutionPlan) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for step in plan.steps:
        outcome = schedule.outcomes.get(step.key)
        steps.append(
            {
                "key": step.key,
                "title": step.title,
                "agent_slug": step.agent_slug,
                "depends_on": list(step.depends_on),
                "status": (
                    "skipped"
                    if step.key in schedule.skipped
                    else "completed"
                    if outcome is not None and outcome.ok
                    else "failed"
                ),
                "attempts": outcome.attempts if outcome else 0,
                "tokens": outcome.tokens if outcome else 0,
                "cost_usd": outcome.cost_usd if outcome else "0",
                "output": (outcome.output or "")[:4_000] if outcome else "",
                "error": outcome.error if outcome else None,
            }
        )
    return steps


async def resume_orchestration(
    db: AsyncSession,
    *,
    task_id: UUID,
    factory: async_sessionmaker[AsyncSession],
    tools: ToolRegistry,
    bus: RedisEventBus | None = None,
    cancel_check: Any = None,
    options: OrchestrationOptions | None = None,
) -> OrchestrationOutcome:
    """Continue a planned orchestration from its persisted plan.

    Used after an approval: the plan and its child tasks already exist, so this
    rebuilds the DAG from ``task.output['plan']`` and re-runs only the steps that
    have not completed yet.
    """
    task = await task_service.get_task(db, task_id)

    if task.status == TaskStatus.WAITING_APPROVAL:
        pending = await db.execute(
            select(Approval.id).where(
                Approval.task_id == task.id,
                Approval.status == ApprovalStatus.PENDING,
            )
        )
        if pending.scalar_one_or_none() is not None:
            raise ConflictError("A decision is still pending for this task")

    if task.status not in {TaskStatus.WAITING_APPROVAL, TaskStatus.PAUSED, TaskStatus.FAILED}:
        raise ConflictError(f"A '{task.status.value}' task cannot be resumed")

    raw_plan = (task.output or {}).get("plan")
    if not raw_plan:
        raise ConflictError("This task has no persisted plan to resume")
    plan = ExecutionPlan.from_dict(raw_plan)
    plan.validate()

    if task.status == TaskStatus.FAILED:
        task.attempts = max(0, task.attempts)
        task.error = None
        task.completed_at = None
        task.status = TaskStatus.PENDING
    if task.status in {TaskStatus.WAITING_APPROVAL, TaskStatus.PAUSED}:
        await task_service.transition_task(
            db, task, TaskStatus.RUNNING, message="Resuming after approval"
        )
    await db.commit()

    return await orchestrate_task(
        db,
        task_id=task_id,
        factory=factory,
        tools=tools,
        bus=bus,
        cancel_check=cancel_check,
        options=options,
        plan_override=plan,
    )
