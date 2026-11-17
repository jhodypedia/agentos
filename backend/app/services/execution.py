"""Task execution: bind the runtime to persistence and the event bus.

Phase 2 runs a task inline within the API request (or a CLI call). Phase 3
moves dispatch onto a worker queue; the interface here already separates
"prepare" from "run" so that change stays local to this module.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.core.events import Event, EventType, RedisEventBus
from app.core.exceptions import NotFoundError, UnprocessableError
from app.core.metrics import COST_TOTAL, TOKENS_TOTAL
from app.db.enums import AgentStatus, ApprovalAction, TaskStatus
from app.db.models import Agent, Approval, ModelProvider, ModelUsage, Task, TaskEvent
from app.providers.base import ChatMessage, ToolCall, Usage
from app.providers.registry import provider_from_record
from app.runtime.loop import run_agent
from app.runtime.types import AgentSpec, RuntimeResult, ToolInvocation
from app.services import agent_context, bounty_tools, research_tools, runtime_settings
from app.services import approvals as approval_service
from app.services import memory as memory_service
from app.services.agents import assigned_tool_slugs, build_agent_spec
from app.services.tasks import get_task, transition_task
from app.tools.base import ToolRegistry
from app.workspace.sandbox import resolve_runner

logger = structlog.get_logger("agentos.services.execution")


class TaskProgressListener:
    """Persist task events and publish them for the dashboard/Telegram.

    Runs in its own session so a long agent run never holds an open
    transaction against the API request's session.
    """

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        task_id: UUID,
        agent_id: UUID,
        bus: RedisEventBus | None = None,
    ) -> None:
        self._factory = factory
        self._task_id = task_id
        self._agent_id = agent_id
        self._bus = bus

    async def _record(
        self,
        event_type: str,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        try:
            async with self._factory() as session:
                session.add(
                    TaskEvent(
                        task_id=self._task_id,
                        agent_id=self._agent_id,
                        event_type=event_type,
                        message=message[:2000],
                        payload=payload or {},
                    )
                )
                await session.commit()
        except Exception:
            logger.exception("task_event_persist_failed", task_id=str(self._task_id))

        if self._bus is None:
            return
        try:
            await self._bus.publish(
                Event(
                    type=_event_type(event_type),
                    task_id=self._task_id,
                    agent_id=self._agent_id,
                    payload={"message": message, **(payload or {})},
                )
            )
        except Exception:
            logger.warning("event_publish_failed", event=event_type)

    async def on_iteration_start(self, iteration: int, message_count: int) -> None:
        await self._record(
            "task.progress",
            f"Iteration {iteration} started with {message_count} messages",
            {"iteration": iteration, "message_count": message_count},
        )

    async def on_assistant_message(
        self, iteration: int, message: ChatMessage, usage: Usage
    ) -> None:
        await self._record(
            "model.usage",
            f"Iteration {iteration}: {usage.total_tokens} tokens (${usage.cost_usd})",
            {
                "iteration": iteration,
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
                "cost_usd": str(usage.cost_usd),
                "tool_calls": [call.name for call in message.tool_calls],
            },
        )

    async def on_tool_started(self, iteration: int, call: ToolCall) -> None:
        await self._record(
            "tool.started",
            f"Calling tool '{call.name}'",
            {"iteration": iteration, "tool": call.name, "arguments": call.arguments},
        )

    async def on_tool_finished(self, iteration: int, invocation: ToolInvocation) -> None:
        await self._record(
            "tool.completed" if invocation.result.ok else "tool.failed",
            f"Tool '{invocation.call.name}' finished",
            {
                "iteration": iteration,
                "tool": invocation.call.name,
                "ok": invocation.result.ok,
                "error": invocation.result.error,
                "duration_ms": invocation.result.duration_ms,
            },
        )

    async def on_run_completed(self, result: RuntimeResult) -> None:
        return None


def _event_type(event_type: str) -> EventType:
    mapping = {
        "task.progress": EventType.TASK_PROGRESS,
        "model.usage": EventType.MODEL_USAGE,
        "tool.started": EventType.TOOL_STARTED,
        "tool.completed": EventType.TOOL_COMPLETED,
        "tool.failed": EventType.TOOL_FAILED,
    }
    return mapping.get(event_type, EventType.TASK_PROGRESS)


class ApprovalRequester:
    """Turn a blocked high-risk tool call into a persisted approval request.

    Runs in its own session and transaction: the agent run must not roll back
    because a sign-off was recorded, and the approval outlives a failed run.
    """

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        task_id: UUID,
        agent_id: UUID,
        requested_by_user_id: UUID | None = None,
    ) -> None:
        self._factory = factory
        self._task_id = task_id
        self._agent_id = agent_id
        self._requested_by = requested_by_user_id
        self.requested: list[Approval] = []

    async def __call__(self, tool: Any, call: Any) -> None:
        async with self._factory() as session:
            if await approval_service.has_pending_tool_approval(
                session, task_id=self._task_id, tool=tool.slug
            ):
                return
            approval = await approval_service.create_approval(
                session,
                title=f"Approve '{tool.slug}' for agent run",
                description=(
                    f"Agent {self._agent_id} wants to run the '{tool.slug}' tool "
                    f"(risk: {tool.risk_level.value}). Arguments: {call.arguments!r}"
                ),
                action_type=ApprovalAction.RUN_HIGH_RISK_TOOL,
                task_id=self._task_id,
                requested_by_agent_id=self._agent_id,
                assigned_to_user_id=None,
                payload={
                    "tool": tool.slug,
                    "arguments": call.arguments,
                    "risk_level": tool.risk_level.value,
                    "tool_call_id": call.id,
                },
            )
            await session.commit()
            await session.refresh(approval)
            self.requested.append(approval)


async def _resolve_provider(db: AsyncSession, agent: Agent) -> Any:
    if agent.model_provider_id is None:
        raise UnprocessableError("The agent has no model provider assigned")
    provider = await db.get(ModelProvider, agent.model_provider_id)
    if provider is None:
        raise UnprocessableError("The agent's model provider no longer exists")
    return provider_from_record(provider)


async def execute_task(
    db: AsyncSession,
    *,
    task_id: UUID,
    tools: ToolRegistry,
    factory: async_sessionmaker[AsyncSession],
    bus: RedisEventBus | None = None,
    cancel_check: Any = None,
    extra_context: str | None = None,
    model_name: str | None = None,
    provider_override: Any | None = None,
    provider_id: UUID | None = None,
) -> RuntimeResult:
    """Run the agent bound to ``task_id`` and persist the outcome.

    ``extra_context`` is appended to the agent's system prompt, which is how a
    delegated step receives the outputs of the steps it depends on.
    ``provider_override``/``model_name`` let an orchestrator run a specialist
    that has no provider or model of its own.
    """
    task = await get_task(db, task_id)
    if task.status in {TaskStatus.RUNNING, TaskStatus.COMPLETED}:
        raise UnprocessableError(f"Task is already {task.status.value}")
    if task.assigned_agent_id is None:
        raise UnprocessableError("The task has no assigned agent")

    agent = await db.get(Agent, task.assigned_agent_id)
    if agent is None:
        raise NotFoundError("The task's agent no longer exists")
    if agent.status != AgentStatus.ACTIVE:
        raise UnprocessableError("The assigned agent is not active")

    tool_slugs = await assigned_tool_slugs(db, agent.id)
    spec: AgentSpec = build_agent_spec(agent, tool_slugs=tool_slugs, model_name=model_name)

    spec.approvals = frozenset(await approval_service.approved_tool_slugs(db, task_id=task.id))

    memory_enabled, _layers = agent_context.memory_settings(agent)
    spec.resources = agent_context.tool_resources(
        factory,
        agent_id=agent.id,
        task_id=task.id,
        project_id=task.project_id,
        user_id=task.created_by,
        memory_enabled=memory_enabled,
    )
    spec.resources.update(
        bounty_tools.makefun_resources(
            factory,
            project_id=task.project_id,
            user_id=task.created_by,
        )
    )
    spec.resources.update(
        research_tools.research_resources(
            factory, project_id=task.project_id, user_id=task.created_by
        )
    )
    workspace = await agent_context.resolve_workspace(db, task.project_id)
    if workspace is not None:
        settings = get_settings()
        report = await resolve_runner(
            sandbox_enabled=await runtime_settings.sandbox_enabled(db, settings),
            sandbox_image=settings.sandbox_image,
            allow_host_execution=settings.sandbox_allow_host_execution,
            memory_limit=settings.sandbox_memory_limit,
            cpu_limit=settings.sandbox_cpu_limit,
            pids_limit=settings.sandbox_pids_limit,
            max_timeout_seconds=settings.sandbox_max_timeout_seconds,
        )
        spec.resources["workspace"] = workspace
        spec.resources["command_runner"] = report.runner
        spec.resources["sandbox"] = report.runner.name
        # The quota is resolved once per run so a settings change mid-task cannot
        # make one tool enforce a different limit than the next.
        spec.metadata["workspace_quota_bytes"] = await runtime_settings.workspace_quota_bytes(
            db, settings
        )
    contextual = await agent_context.build_extra_context(db, agent=agent, task=task)
    if contextual:
        extra_context = f"{extra_context}\n\n{contextual}" if extra_context else contextual

    provider = provider_override or await _resolve_provider(db, agent)
    if provider_id is None:
        provider_id = agent.model_provider_id

    if task.status == TaskStatus.WAITING_APPROVAL:
        await transition_task(
            db,
            task,
            TaskStatus.RUNNING,
            message="Task resumed after human approval",
            payload={"approved_tools": sorted(spec.approvals)},
        )
    elif task.status == TaskStatus.PENDING:
        await transition_task(
            db,
            task,
            TaskStatus.QUEUED,
            message="Task queued for execution",
            payload={"agent_id": str(agent.id)},
        )

    if task.status != TaskStatus.RUNNING:
        await transition_task(
            db,
            task,
            TaskStatus.RUNNING,
            message=f"Task started by agent '{agent.slug}'",
            payload={"agent_id": str(agent.id), "model": agent.model_name},
        )
    await db.commit()

    listener = TaskProgressListener(factory, task_id=task.id, agent_id=agent.id, bus=bus)
    approval_requester = ApprovalRequester(
        factory,
        task_id=task.id,
        agent_id=agent.id,
        requested_by_user_id=task.created_by,
    )
    prompt = task.objective
    if task.description and task.description.strip() != task.objective.strip():
        prompt = f"{task.objective}\n\nContext:\n{task.description}"

    budget_usd = Decimal(task.budget_usd) if task.budget_usd is not None else None
    try:
        result = await run_agent(
            provider=provider,
            spec=spec,
            prompt=prompt,
            tools=tools,
            listener=listener,
            cancel_check=cancel_check,
            approval_callback=approval_requester,
            max_cost_usd=budget_usd,
            extra_context=extra_context,
        )
    finally:
        close = getattr(provider, "aclose", None)
        if close is not None:
            await close()

    await _persist_outcome(
        db, task=task, agent=agent, spec=spec, result=result, provider_id=provider_id
    )
    return result


async def _persist_outcome(
    db: AsyncSession,
    *,
    task: Task,
    agent: Agent,
    spec: AgentSpec,
    result: RuntimeResult,
    provider_id: UUID | None = None,
) -> None:
    await db.refresh(task)
    task.used_tokens = task.used_tokens + result.usage.total_tokens
    task.cost_usd = Decimal(task.cost_usd) + result.usage.cost_usd
    task.used_iterations = task.used_iterations + result.iterations
    task.output = {
        "content": result.content,
        "finish_reason": str(result.finish_reason),
        "iterations": result.iterations,
        "tool_invocations": result.tool_log(),
    }
    if result.error:
        task.error = result.error

    usage_row = ModelUsage(
        provider_id=provider_id,
        agent_id=agent.id,
        task_id=task.id,
        model_name=spec.model,
        prompt_tokens=result.usage.prompt_tokens,
        completion_tokens=result.usage.completion_tokens,
        total_tokens=result.usage.total_tokens,
        cost_usd=result.usage.cost_usd,
        status="error" if result.error else "success",
        error=result.error,
        meta={"iterations": result.iterations, "finish_reason": str(result.finish_reason)},
    )
    db.add(usage_row)
    # Token/cost metrics are labelled by model so a dashboard can attribute spend
    # without joining back to the usage table.
    TOKENS_TOTAL.inc(result.usage.prompt_tokens, model=spec.model, kind="prompt")
    TOKENS_TOTAL.inc(result.usage.completion_tokens, model=spec.model, kind="completion")
    COST_TOTAL.inc(float(result.usage.cost_usd), model=spec.model)

    if result.pending_approvals:
        await transition_task(
            db,
            task,
            TaskStatus.WAITING_APPROVAL,
            message="Task paused: high-risk tool calls need human approval",
            payload={"pending_approvals": result.pending_approvals},
        )
    elif result.error:
        await transition_task(
            db,
            task,
            TaskStatus.FAILED,
            message=result.error,
            payload={"finish_reason": str(result.finish_reason)},
        )
    else:
        await transition_task(
            db,
            task,
            TaskStatus.COMPLETED,
            message="Task completed",
            payload={
                "finish_reason": str(result.finish_reason),
                "iterations": result.iterations,
                "total_tokens": result.usage.total_tokens,
                "cost_usd": str(result.usage.cost_usd),
            },
        )
        # Remember what the run produced. The write runs inside a savepoint so a
        # failure discards only the memory row: the task is already completed and
        # must not be rolled back with it.
        try:
            async with db.begin_nested():
                await memory_service.record_task_outcome(db, task)
        except Exception as exc:
            logger.warning("task_outcome_memory_failed", task_id=str(task.id), error=str(exc))
    await db.commit()


async def pending_tasks(db: AsyncSession, limit: int = 10) -> list[Task]:
    result = await db.execute(
        select(Task)
        .where(Task.status.in_([TaskStatus.PENDING, TaskStatus.QUEUED]))
        .order_by(Task.priority.asc(), Task.created_at.asc())
        .limit(limit)
    )
    return list(result.scalars())


def utcnow() -> datetime:
    return datetime.now(UTC)
