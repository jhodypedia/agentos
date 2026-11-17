"""The reusable agent runtime loop.

One run is: assemble context -> ask the model -> execute requested tools ->
feed results back -> repeat until the model finishes, a limit is hit, or the
caller cancels. Every limit is enforced here, not by the model.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from decimal import Decimal
from typing import Any

import structlog

from app.core.exceptions import AppError
from app.core.metrics import TOOL_DURATION, TOOL_RUNS
from app.db.enums import RiskLevel
from app.providers.base import ChatMessage, LLMRequest, LLMResponse, ToolSpec, Usage
from app.providers.errors import ProviderError
from app.runtime.errors import (
    BudgetExceededError,
    IterationLimitError,
    TaskCancelledError,
    TaskTimeoutError,
)
from app.runtime.types import (
    AgentSpec,
    NullListener,
    RuntimeListener,
    RuntimeResult,
    ToolInvocation,
)
from app.tools.base import Tool, ToolRegistry, ToolResult
from app.tools.errors import (
    ApprovalRequiredError,
    ToolExecutionError,
    ToolPermissionError,
    ToolValidationError,
)

logger = structlog.get_logger("agentos.runtime")

CancelCheck = Callable[[], Awaitable[bool]]
ApprovalCallback = Callable[[Tool, Any], Awaitable[None]]


class _ToolCallBroker:
    """Execute model-requested tools with declaration-level enforcement."""

    def __init__(self, registry: ToolRegistry, spec: AgentSpec) -> None:
        self._registry = registry
        self._spec = spec

    def resolve(self, name: str) -> Tool:
        tool = self._registry.get(name)
        if self._spec.tool_slugs is not None and tool.slug not in self._spec.tool_slugs:
            raise ToolPermissionError(f"Tool '{name}' is not assigned to this agent")
        return tool

    def available_specs(self) -> list[ToolSpec]:
        context = self._spec.tool_context()
        tools: Sequence[Tool] = self._registry.allowed_for(context, include_approval_gated=True)
        if self._spec.tool_slugs is not None:
            tools = [tool for tool in tools if tool.slug in self._spec.tool_slugs]
        return [
            ToolSpec(name=tool.slug, description=tool.description, parameters=tool.parameters)
            for tool in sorted(tools, key=lambda item: item.slug)
        ]


async def run_agent(
    *,
    provider: Any,
    spec: AgentSpec,
    prompt: str,
    tools: ToolRegistry,
    history: Sequence[ChatMessage] | None = None,
    listener: RuntimeListener | None = None,
    cancel_check: CancelCheck | None = None,
    approval_callback: ApprovalCallback | None = None,
    max_cost_usd: Decimal | None = None,
    extra_context: str | None = None,
) -> RuntimeResult:
    """Execute one agent run and return its result.

    ``provider`` only needs an awaitable ``complete(LLMRequest) -> LLMResponse``.
    """
    events: RuntimeListener = listener or NullListener()
    broker = _ToolCallBroker(tools, spec)
    pending_approvals: list[dict[str, Any]] = []
    tool_specs = broker.available_specs()

    system_parts = [spec.system_prompt.strip()] if spec.system_prompt.strip() else []
    if extra_context:
        system_parts.append(extra_context.strip())

    messages: list[ChatMessage] = []
    if system_parts:
        messages.append(ChatMessage(role="system", content="\n\n".join(system_parts)))
    for message in history or ():
        messages.append(message)
    messages.append(ChatMessage(role="user", content=prompt))

    usage = Usage()
    invocations: list[ToolInvocation] = []
    content = ""
    finish_reason = "stop"
    iterations = 0
    started = time.monotonic()
    deadline = started + spec.timeout_seconds

    try:
        for iteration in range(1, spec.max_iterations + 1):
            _check_deadline(deadline)
            if cancel_check is not None and await cancel_check():
                raise TaskCancelledError()

            iterations = iteration
            await events.on_iteration_start(iteration, len(messages))

            request = LLMRequest(
                model=spec.model,
                messages=list(messages),
                tools=tool_specs,
                temperature=spec.temperature,
                top_p=spec.top_p,
                max_output_tokens=spec.max_output_tokens,
                timeout_seconds=max(5.0, min(300.0, deadline - time.monotonic())),
            )
            response: LLMResponse = await provider.complete(request)
            usage = usage.merged(response.usage)

            if usage.total_tokens > spec.token_budget:
                raise BudgetExceededError(
                    f"Token budget of {spec.token_budget} exceeded",
                    details={
                        "used_tokens": usage.total_tokens,
                        "budget_tokens": spec.token_budget,
                    },
                )
            if max_cost_usd is not None and usage.cost_usd > max_cost_usd:
                raise BudgetExceededError(
                    f"Cost budget of ${max_cost_usd} exceeded",
                    details={"used_usd": str(usage.cost_usd), "budget_usd": str(max_cost_usd)},
                )

            content = response.content
            finish_reason = response.finish_reason
            assistant = ChatMessage(
                role="assistant", content=response.content, tool_calls=response.tool_calls
            )
            messages.append(assistant)
            await events.on_assistant_message(iteration, assistant, response.usage)

            if not response.tool_calls:
                break

            for call in response.tool_calls:
                _check_deadline(deadline)
                if cancel_check is not None and await cancel_check():
                    raise TaskCancelledError()
                await events.on_tool_started(iteration, call)
                tool_result = await _execute_tool(
                    broker,
                    call,
                    spec,
                    iteration,
                    approval_callback=approval_callback,
                    pending_approvals=pending_approvals,
                )
                invocation = ToolInvocation(call=call, result=tool_result, iteration=iteration)
                invocations.append(invocation)
                messages.append(invocation.to_message())
                await events.on_tool_finished(iteration, invocation)
        else:
            raise IterationLimitError(
                f"The agent reached its limit of {spec.max_iterations} iterations"
            )
    except (IterationLimitError, BudgetExceededError, TaskCancelledError, TaskTimeoutError) as exc:
        logger.warning("agent_run_aborted", agent=spec.slug, reason=exc.code)
        result = RuntimeResult(
            content=content,
            messages=messages,
            usage=usage,
            iterations=iterations,
            finish_reason=finish_reason,
            tool_invocations=invocations,
            error=exc.message,
            pending_approvals=pending_approvals,
        )
        await events.on_run_completed(result)
        return result
    except ProviderError as exc:
        logger.error("provider_failure", agent=spec.slug, error=str(exc))
        result = RuntimeResult(
            content=content,
            messages=messages,
            usage=usage,
            iterations=iterations,
            finish_reason="error",
            tool_invocations=invocations,
            error=str(exc),
            pending_approvals=pending_approvals,
        )
        await events.on_run_completed(result)
        return result
    except AppError:
        raise
    except Exception as exc:
        logger.exception("agent_run_failed", agent=spec.slug)
        result = RuntimeResult(
            content=content,
            messages=messages,
            usage=usage,
            iterations=iterations,
            finish_reason="error",
            tool_invocations=invocations,
            error=f"{type(exc).__name__}: {exc}",
            pending_approvals=pending_approvals,
        )
        await events.on_run_completed(result)
        return result

    result = RuntimeResult(
        content=content,
        messages=messages,
        usage=usage,
        iterations=iterations,
        finish_reason=finish_reason,
        tool_invocations=invocations,
        pending_approvals=pending_approvals,
    )
    await events.on_run_completed(result)
    return result


async def _execute_tool(
    broker: _ToolCallBroker,
    call: Any,
    spec: AgentSpec,
    iteration: int,
    *,
    approval_callback: ApprovalCallback | None = None,
    pending_approvals: list[dict[str, Any]] | None = None,
) -> ToolResult:
    started = time.perf_counter()
    try:
        tool = broker.resolve(call.name)
    except (ToolPermissionError, ToolExecutionError) as exc:
        return ToolResult(ok=False, error=str(exc))
    except AppError as exc:
        return ToolResult(ok=False, error=exc.message)

    context = spec.tool_context()
    try:
        result = await tool.run(call.arguments, context)
    except ApprovalRequiredError as exc:
        # The tool is assigned but gated: record the request so a human can
        # decide, and tell the model plainly that it did not run.
        if pending_approvals is not None:
            pending_approvals.append(
                {
                    "tool": tool.slug,
                    "arguments": call.arguments,
                    "risk_level": tool.risk_level.value,
                    "reason": exc.message,
                }
            )
        if approval_callback is not None:
            try:
                await approval_callback(tool, call)
            except Exception:
                logger.exception("approval_request_failed", tool=tool.slug, agent=spec.slug)
        TOOL_RUNS.inc(tool=tool.slug, outcome="approval_required")
        return ToolResult(
            ok=False,
            error=(
                f"Tool '{tool.slug}' requires human approval and did not run. "
                "An approval request has been recorded for a human to decide."
            ),
        )
    except (ToolPermissionError, ToolValidationError, ToolExecutionError) as exc:
        TOOL_RUNS.inc(tool=tool.slug, outcome="refused")
        return ToolResult(
            ok=False,
            error=exc.message,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )

    if tool.risk_level is RiskLevel.CRITICAL:
        logger.warning("critical_tool_executed", tool=tool.slug, agent=spec.slug)
    TOOL_DURATION.observe(result.duration_ms / 1000, tool=tool.slug)
    TOOL_RUNS.inc(tool=tool.slug, outcome="ok" if result.ok else "error")
    return result


def _check_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise TaskTimeoutError("The agent exceeded its execution timeout")


def default_tool_context_permissions() -> frozenset[str]:
    """Permissions granted to a fresh agent: read-only tools only."""
    return frozenset({"tool:read"})


async def gather_limited(coros: Sequence[Awaitable[Any]], limit: int) -> list[Any]:
    """Run awaitables with a concurrency cap, preserving order."""
    if limit < 1:
        raise ValueError("limit must be at least 1")
    semaphore = asyncio.Semaphore(limit)

    async def _run(coro: Awaitable[Any]) -> Any:
        async with semaphore:
            return await coro

    return await asyncio.gather(*(_run(coro) for coro in coros))
