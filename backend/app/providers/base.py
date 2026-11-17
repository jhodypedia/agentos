"""Provider-neutral types shared by every model adapter."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

MessageRole = Literal["system", "user", "assistant", "tool"]
FinishReason = Literal["stop", "length", "tool_calls", "content_filter", "error"]


@dataclass(slots=True)
class ToolCall:
    """A model's request to invoke a tool."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ChatMessage:
    """One provider-neutral conversation turn."""

    role: MessageRole
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None

    def to_openai(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": _dumps(call.arguments)},
                }
                for call in self.tool_calls
            ]
            payload["content"] = self.content or None
        if self.tool_call_id is not None:
            payload["tool_call_id"] = self.tool_call_id
        if self.name is not None:
            payload["name"] = self.name
        return payload


@dataclass(slots=True)
class ToolSpec:
    """A tool definition advertised to the model."""

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})

    def to_openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass(slots=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: Decimal = Decimal("0")

    def merged(self, other: Usage) -> Usage:
        return Usage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
        )


@dataclass(slots=True)
class LLMRequest:
    """A single completion request."""

    model: str
    messages: list[ChatMessage]
    tools: list[ToolSpec] = field(default_factory=list)
    temperature: float = 0.2
    top_p: float = 1.0
    max_output_tokens: int = 4096
    timeout_seconds: float = 120.0
    json_mode: bool = False
    stop: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class LLMResponse:
    """A normalised completion response."""

    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    finish_reason: FinishReason = "stop"
    model: str = ""
    request_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ProviderHealth:
    ok: bool
    detail: str = ""
    models: list[str] = field(default_factory=list)
    latency_ms: int | None = None


def _dumps(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
