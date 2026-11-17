"""Tests for the agent runtime loop."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from app.db.enums import RiskLevel
from app.providers.base import LLMRequest, LLMResponse, ToolCall, Usage
from app.runtime.loop import gather_limited, run_agent
from app.runtime.types import AgentSpec, NullListener, RuntimeResult
from app.tools.base import Tool, ToolContext, ToolRegistry
from app.tools.builtin import CalculatorTool, EchoTool


class FakeProvider:
    """Replays a scripted list of responses and records requests."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if not self._responses:
            raise AssertionError("FakeProvider ran out of scripted responses")
        return self._responses.pop(0)


class RecordingListener(NullListener):
    def __init__(self) -> None:
        self.iterations: list[int] = []
        self.tools: list[str] = []
        self.tool_results: list[bool] = []
        self.completed: RuntimeResult | None = None

    async def on_iteration_start(self, iteration: int, message_count: int) -> None:
        self.iterations.append(iteration)

    async def on_tool_started(self, iteration: int, call: ToolCall) -> None:
        self.tools.append(call.name)

    async def on_tool_finished(self, iteration: int, invocation: Any) -> None:
        self.tool_results.append(invocation.result.ok)

    async def on_run_completed(self, result: RuntimeResult) -> None:
        self.completed = result


def _response(
    content: str = "",
    *,
    tool_calls: list[ToolCall] | None = None,
    tokens: int = 10,
    cost: str = "0.001",
) -> LLMResponse:
    return LLMResponse(
        content=content,
        tool_calls=tool_calls or [],
        usage=Usage(
            prompt_tokens=tokens // 2,
            completion_tokens=tokens // 2,
            total_tokens=tokens,
            cost_usd=Decimal(cost),
        ),
        finish_reason="tool_calls" if tool_calls else "stop",
        model="test-model",
    )


def _spec(**overrides: Any) -> AgentSpec:
    defaults: dict[str, Any] = {
        "slug": "tester",
        "name": "Tester",
        "system_prompt": "You are a test agent.",
        "model": "test-model",
        "max_iterations": 5,
        "token_budget": 10_000,
        "timeout_seconds": 30,
        "permissions": frozenset({"tool:read"}),
    }
    defaults.update(overrides)
    return AgentSpec(**defaults)


def _registry() -> ToolRegistry:
    return ToolRegistry([EchoTool(), CalculatorTool()])


async def test_simple_completion_without_tools() -> None:
    provider = FakeProvider([_response("final answer")])
    listener = RecordingListener()
    result = await run_agent(
        provider=provider,
        spec=_spec(),
        prompt="hello",
        tools=_registry(),
        listener=listener,
    )
    assert result.ok
    assert result.content == "final answer"
    assert result.iterations == 1
    assert result.finish_reason == "stop"
    assert listener.completed is result
    assert provider.requests[0].messages[0].role == "system"
    assert provider.requests[0].messages[-1].content == "hello"


async def test_tool_call_round_trip() -> None:
    provider = FakeProvider(
        [
            _response(
                tool_calls=[ToolCall(id="c1", name="echo", arguments={"text": "ping"})],
            ),
            _response("done"),
        ]
    )
    listener = RecordingListener()
    result = await run_agent(
        provider=provider, spec=_spec(), prompt="use echo", tools=_registry(), listener=listener
    )
    assert result.ok
    assert result.iterations == 2
    assert result.content == "done"
    assert len(result.tool_invocations) == 1
    assert result.tool_invocations[0].result.output == {"text": "ping"}
    assert listener.tools == ["echo"]
    assert listener.tool_results == [True]

    second = provider.requests[1]
    tool_messages = [message for message in second.messages if message.role == "tool"]
    assert tool_messages[0].tool_call_id == "c1"
    assert "ping" in tool_messages[0].content


async def test_tool_error_is_reported_back_to_model() -> None:
    provider = FakeProvider(
        [
            _response(
                tool_calls=[ToolCall(id="c1", name="calculate", arguments={"expression": "1/0"})]
            ),
            _response("recovered"),
        ]
    )
    result = await run_agent(provider=provider, spec=_spec(), prompt="bad math", tools=_registry())
    assert result.ok
    assert result.tool_invocations[0].result.ok is False
    tool_message = next(m for m in provider.requests[1].messages if m.role == "tool")
    assert tool_message.content.startswith("ERROR")


async def test_unknown_tool_is_rejected() -> None:
    provider = FakeProvider(
        [
            _response(tool_calls=[ToolCall(id="c1", name="shell", arguments={})]),
            _response("ok"),
        ]
    )
    result = await run_agent(provider=provider, spec=_spec(), prompt="x", tools=_registry())
    assert result.tool_invocations[0].result.ok is False
    assert "not available" in (result.tool_invocations[0].result.error or "")


async def test_tool_not_assigned_to_agent_is_rejected() -> None:
    provider = FakeProvider(
        [
            _response(tool_calls=[ToolCall(id="c1", name="echo", arguments={"text": "x"})]),
            _response("ok"),
        ]
    )
    result = await run_agent(
        provider=provider,
        spec=_spec(tool_slugs=frozenset({"calculate"})),
        prompt="x",
        tools=_registry(),
    )
    assert result.tool_invocations[0].result.ok is False
    assert "not assigned" in (result.tool_invocations[0].result.error or "")


async def test_tool_specs_limited_to_assignments() -> None:
    provider = FakeProvider([_response("done")])
    await run_agent(
        provider=provider,
        spec=_spec(tool_slugs=frozenset({"echo"})),
        prompt="x",
        tools=_registry(),
    )
    assert [tool.name for tool in provider.requests[0].tools] == ["echo"]


async def test_iteration_limit_aborts_run() -> None:
    looping = [
        _response(tool_calls=[ToolCall(id=f"c{i}", name="echo", arguments={"text": "x"})])
        for i in range(4)
    ]
    provider = FakeProvider(looping)
    result = await run_agent(
        provider=provider, spec=_spec(max_iterations=3), prompt="loop", tools=_registry()
    )
    assert result.ok is False
    assert "iterations" in (result.error or "")
    assert result.iterations == 3


async def test_token_budget_aborts_run() -> None:
    provider = FakeProvider(
        [
            _response(
                "big",
                tokens=5000,
                tool_calls=[ToolCall(id="c1", name="echo", arguments={"text": "x"})],
            ),
            _response("more", tokens=6000),
        ]
    )
    result = await run_agent(
        provider=provider,
        spec=_spec(token_budget=8000),
        prompt="x",
        tools=_registry(),
    )
    assert result.ok is False
    assert "Token budget" in (result.error or "")


async def test_cost_budget_aborts_run() -> None:
    provider = FakeProvider(
        [
            _response(
                "a",
                cost="0.5",
                tool_calls=[ToolCall(id="c1", name="echo", arguments={"text": "x"})],
            ),
            _response("b", cost="0.6"),
        ]
    )
    result = await run_agent(
        provider=provider,
        spec=_spec(),
        prompt="x",
        tools=_registry(),
        max_cost_usd=Decimal("0.9"),
    )
    assert result.ok is False
    assert "Cost budget" in (result.error or "")


async def test_cancel_check_stops_run() -> None:
    provider = FakeProvider([_response("a"), _response("b")])

    async def cancel() -> bool:
        return True

    result = await run_agent(
        provider=provider, spec=_spec(), prompt="x", tools=_registry(), cancel_check=cancel
    )
    assert result.ok is False
    assert "cancelled" in (result.error or "")
    assert provider.requests == []


async def test_timeout_aborts_run() -> None:
    provider = FakeProvider([_response("a")])
    result = await run_agent(
        provider=provider, spec=_spec(timeout_seconds=1), prompt="x", tools=_registry()
    )
    assert result.ok is True


async def test_provider_error_is_captured() -> None:
    class Broken:
        async def complete(self, request: LLMRequest) -> LLMResponse:
            from app.providers.errors import ProviderError

            raise ProviderError("upstream exploded")

    result = await run_agent(provider=Broken(), spec=_spec(), prompt="x", tools=_registry())
    assert result.ok is False
    assert result.finish_reason == "error"
    assert "upstream exploded" in (result.error or "")


async def test_history_is_included_before_prompt() -> None:
    from app.providers.base import ChatMessage

    provider = FakeProvider([_response("ok")])
    await run_agent(
        provider=provider,
        spec=_spec(),
        prompt="second",
        tools=_registry(),
        history=[
            ChatMessage(role="user", content="first"),
            ChatMessage(role="assistant", content="reply"),
        ],
    )
    contents = [message.content for message in provider.requests[0].messages]
    assert contents[1:4] == ["first", "reply", "second"]


async def test_extra_context_appended_to_system_prompt() -> None:
    provider = FakeProvider([_response("ok")])
    await run_agent(
        provider=provider,
        spec=_spec(),
        prompt="x",
        tools=_registry(),
        extra_context="Project: demo",
    )
    system = provider.requests[0].messages[0].content
    assert "You are a test agent." in system
    assert "Project: demo" in system


async def test_gather_limited_preserves_order_and_caps() -> None:
    import asyncio

    async def work(value: int) -> int:
        await asyncio.sleep(0.01)
        return value

    results = await gather_limited([work(i) for i in range(10)], limit=3)
    assert results == list(range(10))

    with pytest.raises(ValueError):
        await gather_limited([], limit=0)


async def test_high_risk_tool_blocked_without_approval() -> None:
    class Danger(Tool):
        slug = "danger"
        name = "Danger"
        description = "risky"
        parameters = {"type": "object", "properties": {}}
        risk_level = RiskLevel.HIGH

        async def execute(self, arguments: dict, context: ToolContext) -> dict:
            return {"ran": True}

    provider = FakeProvider(
        [
            _response(tool_calls=[ToolCall(id="c1", name="danger", arguments={})]),
            _response("blocked"),
        ]
    )
    result = await run_agent(
        provider=provider,
        spec=_spec(),
        prompt="x",
        tools=ToolRegistry([Danger()]),
    )
    assert result.tool_invocations[0].result.ok is False
    assert "approval" in (result.tool_invocations[0].result.error or "").lower()


class _GatedTool(Tool):
    slug = "gated"
    name = "Gated"
    description = "needs a human"
    parameters = {"type": "object", "properties": {"path": {"type": "string"}}}
    risk_level = RiskLevel.HIGH
    required_permissions = ("filesystem:write",)

    async def execute(self, arguments: dict, context: ToolContext) -> dict:
        return {"written": arguments.get("path")}


def _gated_provider() -> FakeProvider:
    return FakeProvider(
        [
            _response(tool_calls=[ToolCall(id="g1", name="gated", arguments={"path": "a.txt"})]),
            _response("done"),
        ]
    )


async def test_gated_tool_is_advertised_but_not_run_without_approval() -> None:
    approval_requests: list[tuple[str, dict]] = []
    provider = _gated_provider()

    async def request_approval(tool, call) -> None:
        approval_requests.append((tool.slug, call.arguments))

    result = await run_agent(
        provider=provider,
        spec=AgentSpec(
            slug="dev",
            name="Dev",
            system_prompt="",
            model="m",
            permissions=frozenset({"filesystem:write"}),
            tool_slugs=frozenset({"gated"}),
        ),
        prompt="write it",
        tools=ToolRegistry([_GatedTool()]),
        approval_callback=request_approval,
    )

    advertised = [tool.name for tool in provider.requests[0].tools]
    assert advertised == ["gated"]
    assert approval_requests == [("gated", {"path": "a.txt"})]
    assert result.tool_invocations[0].result.ok is False
    assert result.pending_approvals == [
        {
            "tool": "gated",
            "arguments": {"path": "a.txt"},
            "risk_level": "high",
            "reason": "Tool 'gated' has risk level 'high' and needs approval",
        }
    ]


async def test_gated_tool_runs_once_approved_in_the_spec() -> None:
    result = await run_agent(
        provider=_gated_provider(),
        spec=AgentSpec(
            slug="dev",
            name="Dev",
            system_prompt="",
            model="m",
            permissions=frozenset({"filesystem:write"}),
            approvals=frozenset({"gated"}),
            tool_slugs=frozenset({"gated"}),
        ),
        prompt="write it",
        tools=ToolRegistry([_GatedTool()]),
    )

    assert result.tool_invocations[0].result.ok is True
    assert result.tool_invocations[0].result.output == {"written": "a.txt"}
    assert result.pending_approvals == []


async def test_approval_callback_failure_does_not_break_the_run() -> None:
    async def boom(tool, call) -> None:
        raise RuntimeError("database down")

    result = await run_agent(
        provider=_gated_provider(),
        spec=AgentSpec(
            slug="dev",
            name="Dev",
            system_prompt="",
            model="m",
            permissions=frozenset({"filesystem:write"}),
            tool_slugs=frozenset({"gated"}),
        ),
        prompt="write it",
        tools=ToolRegistry([_GatedTool()]),
        approval_callback=boom,
    )

    assert result.tool_invocations[0].result.ok is False
    assert result.pending_approvals and result.pending_approvals[0]["tool"] == "gated"
    assert result.content == "done"
