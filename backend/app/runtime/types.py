"""Runtime data structures shared by the loop, services and tests."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID

from app.providers.base import ChatMessage, FinishReason, ToolCall, Usage
from app.tools.base import ToolContext, ToolResult


@dataclass(slots=True)
class AgentSpec:
    """The execution-relevant subset of an agent definition."""

    slug: str
    name: str
    system_prompt: str
    model: str
    temperature: float = 0.2
    top_p: float = 1.0
    max_output_tokens: int = 4096
    max_iterations: int = 25
    token_budget: int = 200_000
    timeout_seconds: int = 1800
    permissions: frozenset[str] = frozenset()
    tool_slugs: frozenset[str] | None = None
    approvals: frozenset[str] = frozenset()
    metadata: dict[str, Any] = field(default_factory=dict)
    resources: dict[str, Any] = field(default_factory=dict)

    def tool_context(
        self,
        *,
        task_id: UUID | None = None,
        step_id: UUID | None = None,
        agent_id: UUID | None = None,
        workspace_root: str | None = None,
    ) -> ToolContext:
        return ToolContext(
            agent_id=agent_id,
            task_id=task_id,
            step_id=step_id,
            workspace_root=workspace_root,
            permissions=self.permissions,
            approvals=self.approvals,
            metadata=dict(self.metadata),
            resources=dict(self.resources),
        )


@dataclass(slots=True)
class ToolInvocation:
    """A record of one tool execution inside a run."""

    call: ToolCall
    result: ToolResult
    iteration: int

    def to_message(self) -> ChatMessage:
        return ChatMessage(
            role="tool",
            content=self.result.as_text(),
            tool_call_id=self.call.id,
            name=self.call.name,
        )


@dataclass(slots=True)
class RuntimeResult:
    """The outcome of one agent run."""

    content: str
    messages: list[ChatMessage]
    usage: Usage
    iterations: int
    finish_reason: FinishReason | str
    tool_invocations: list[ToolInvocation] = field(default_factory=list)
    error: str | None = None
    # High-risk calls the model requested but may not run without human sign-off.
    pending_approvals: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error is None

    def tool_log(self) -> list[dict[str, Any]]:
        return [
            {
                "iteration": invocation.iteration,
                "tool": invocation.call.name,
                "arguments": invocation.call.arguments,
                "ok": invocation.result.ok,
                "error": invocation.result.error,
                "duration_ms": invocation.result.duration_ms,
            }
            for invocation in self.tool_invocations
        ]


class RuntimeListener(Protocol):
    """Observer hooks. Implementations must not raise."""

    async def on_iteration_start(self, iteration: int, message_count: int) -> None: ...

    async def on_assistant_message(
        self, iteration: int, message: ChatMessage, usage: Usage
    ) -> None: ...

    async def on_tool_started(self, iteration: int, call: ToolCall) -> None: ...

    async def on_tool_finished(self, iteration: int, invocation: ToolInvocation) -> None: ...

    async def on_run_completed(self, result: RuntimeResult) -> None: ...


class NullListener:
    """No-op listener used when the caller does not need progress events."""

    async def on_iteration_start(self, iteration: int, message_count: int) -> None:
        return None

    async def on_assistant_message(
        self, iteration: int, message: ChatMessage, usage: Usage
    ) -> None:
        return None

    async def on_tool_started(self, iteration: int, call: ToolCall) -> None:
        return None

    async def on_tool_finished(self, iteration: int, invocation: ToolInvocation) -> None:
        return None

    async def on_run_completed(self, result: RuntimeResult) -> None:
        return None


def estimate_tokens(text: str) -> int:
    """Cheap token estimate (about four characters per token)."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def render_tool_result(result: ToolResult) -> str:
    if not result.ok:
        return f"ERROR: {result.error or 'tool failed'}"
    return json.dumps(result.output, ensure_ascii=False, default=str)
