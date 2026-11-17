"""Unit tests for execution plans and DAG scheduling."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.core.exceptions import UnprocessableError
from app.orchestrator.plan import ExecutionPlan, PlanStep
from app.orchestrator.planner import (
    PLAN_TOOL,
    LlmPlanner,
    plan_from_response,
    render_dependency_context,
    single_step_plan,
)
from app.orchestrator.scheduler import StepOutcome, aggregate_output, run_plan
from app.providers.base import LLMResponse, ToolCall
from app.providers.errors import ProviderError


def _step(key: str, *, deps: tuple[str, ...] = (), agent: str = "research") -> PlanStep:
    return PlanStep(
        key=key, title=f"Step {key}", instruction=f"Do {key}", agent_slug=agent, depends_on=deps
    )


def _plan(*steps: PlanStep, objective: str = "Ship it") -> ExecutionPlan:
    return ExecutionPlan(objective=objective, steps=list(steps))


# ------------------------------------------------------------------------ plan
def test_plan_rejects_duplicate_keys() -> None:
    with pytest.raises(UnprocessableError, match="Duplicate step key"):
        _plan(_step("a"), _step("a")).validate()


def test_plan_rejects_unknown_dependency_and_self_reference() -> None:
    with pytest.raises(UnprocessableError, match="unknown step"):
        _plan(_step("a", deps=("nope",))).validate()

    with pytest.raises(UnprocessableError, match="cannot depend on itself"):
        _plan(_step("a", deps=("a",))).validate()


def test_plan_rejects_cycles() -> None:
    plan = _plan(_step("a", deps=("b",)), _step("b", deps=("a",)))
    with pytest.raises(UnprocessableError, match="cycle"):
        plan.validate()


def test_plan_rejects_unknown_agent() -> None:
    plan = _plan(_step("a", agent="ghost"))
    with pytest.raises(UnprocessableError, match="unknown agent"):
        plan.validate(known_agents={"research"})


def test_plan_requires_agent_for_agent_mode() -> None:
    step = PlanStep(key="a", title="A", instruction="x", agent_slug=None)
    with pytest.raises(UnprocessableError, match="needs an agent_slug"):
        _plan(step).validate()


def test_plan_requires_objective_and_steps() -> None:
    with pytest.raises(UnprocessableError, match="non-empty objective"):
        ExecutionPlan(objective="   ", steps=[_step("a")]).validate()
    with pytest.raises(UnprocessableError, match="at least one step"):
        ExecutionPlan(objective="x", steps=[]).validate()


def test_plan_levels_group_independent_steps() -> None:
    plan = _plan(
        _step("scan"),
        _step("audit", deps=("scan",)),
        _step("docs"),
        _step("report", deps=("audit", "docs")),
    ).validate()

    levels = plan.levels()
    assert [[step.key for step in level] for level in levels] == [
        ["docs", "scan"],
        ["audit"],
        ["report"],
    ]


def test_plan_normalises_keys_and_deduplicates_dependencies() -> None:
    plan = _plan(
        _step("Scan", deps=("Docs", "Docs"), agent="Research"),
        _step("docs", agent="Research"),
    ).validate()

    assert [step.key for step in plan.steps] == ["scan", "docs"]
    assert plan.steps[0].agent_slug == "research"
    assert plan.steps[0].depends_on == ("docs",)


def test_plan_round_trips_through_dict() -> None:
    plan = _plan(_step("a"), _step("b", deps=("a",)), objective="Obj").validate()
    restored = ExecutionPlan.from_dict(plan.to_dict()).validate()
    assert restored.to_dict() == plan.to_dict()


def test_plan_from_dict_rejects_non_list_steps() -> None:
    with pytest.raises(UnprocessableError, match="must be a list"):
        ExecutionPlan.from_dict({"objective": "x", "steps": {"a": 1}})


# ------------------------------------------------------------------ planner
def test_single_step_plan_uses_assigned_agent() -> None:
    plan = single_step_plan(objective="Do the thing", agent_slug="software-developer")
    assert plan.steps[0].agent_slug == "software-developer"
    assert plan.steps[0].instruction == "Do the thing"
    assert plan.metadata["strategy"] == "single_step"


def test_plan_from_response_prefers_submit_plan_tool_call() -> None:
    response = LLMResponse(
        content="",
        tool_calls=[
            ToolCall(
                id="1",
                name=PLAN_TOOL.name,
                arguments={
                    "rationale": "two steps",
                    "steps": [
                        {"key": "a", "title": "A", "instruction": "do a", "agent_slug": "research"},
                        {
                            "key": "b",
                            "title": "B",
                            "instruction": "do b",
                            "agent_slug": "report-writer",
                            "depends_on": ["a"],
                        },
                    ],
                },
            )
        ],
    )
    plan = plan_from_response(response)
    assert plan is not None
    assert [step.key for step in plan.steps] == ["a", "b"]
    assert plan.rationale == "two steps"


def test_plan_from_response_parses_fenced_json() -> None:
    response = LLMResponse(
        content='Here you go:\n```json\n{"steps": [{"key": "a", "title": "A", '
        '"instruction": "x", "agent_slug": "research"}]}\n```',
    )
    plan = plan_from_response(response)
    assert plan is not None
    assert plan.steps[0].key == "a"


def test_plan_from_response_returns_none_for_garbage() -> None:
    assert plan_from_response(LLMResponse(content="no json here")) is None


class _Provider:
    def __init__(self, response: LLMResponse | Exception) -> None:
        self._response = response

    async def complete(self, request: Any) -> LLMResponse:
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


@pytest.mark.asyncio
async def test_llm_planner_returns_valid_plan() -> None:
    provider = _Provider(
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id="1",
                    name=PLAN_TOOL.name,
                    arguments={
                        "steps": [
                            {
                                "key": "scan",
                                "title": "Scan",
                                "instruction": "Scan the repo",
                                "agent_slug": "repo-analysis",
                            }
                        ]
                    },
                )
            ],
        )
    )
    planner = LlmPlanner(
        provider=provider,
        model="test-model",
        agent_slug="main-orchestrator",
        available_agents={"repo-analysis": "Repo analysis"},
    )
    plan = await planner.plan(objective="Audit repo")
    assert plan.metadata["strategy"] == "llm"
    assert plan.steps[0].max_iterations == 30


@pytest.mark.asyncio
async def test_llm_planner_falls_back_on_provider_error() -> None:
    planner = LlmPlanner(
        provider=_Provider(ProviderError("boom")),
        model="test-model",
        agent_slug="main-orchestrator",
        available_agents={"main-orchestrator": "orchestrator"},
    )
    plan = await planner.plan(objective="Do it")
    assert plan.metadata["fallback_reason"] == "provider_error"
    assert len(plan.steps) == 1
    assert plan.steps[0].agent_slug == "main-orchestrator"


@pytest.mark.asyncio
async def test_llm_planner_falls_back_on_invalid_plan() -> None:
    provider = _Provider(
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id="1",
                    name=PLAN_TOOL.name,
                    arguments={
                        "steps": [
                            {
                                "key": "a",
                                "title": "A",
                                "instruction": "x",
                                "agent_slug": "unknown-agent",
                            }
                        ]
                    },
                )
            ],
        )
    )
    planner = LlmPlanner(
        provider=provider,
        model="test-model",
        agent_slug="main-orchestrator",
        available_agents={"research": "Research"},
    )
    plan = await planner.plan(objective="Do it")
    assert plan.metadata["fallback_reason"] == "validation_failed"


def test_render_dependency_context_includes_only_dependencies() -> None:
    step = _step("report", deps=("a", "b"))
    rendered = render_dependency_context(step, {"a": "alpha", "b": "", "c": "gamma"})
    assert "alpha" in rendered
    assert "no output produced" in rendered
    assert "gamma" not in rendered
    assert render_dependency_context(_step("solo"), {"a": "x"}) == ""


# ------------------------------------------------------------------ scheduler
def _runner(outputs: dict[str, Any]):
    calls: list[tuple[str, int]] = []

    async def runner(step: PlanStep, attempt: int) -> StepOutcome:
        calls.append((step.key, attempt))
        outcome = outputs[step.key]
        if isinstance(outcome, Exception):
            raise outcome
        if callable(outcome):
            return await outcome(attempt)
        return StepOutcome(ok=True, output=str(outcome))

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


@pytest.mark.asyncio
async def test_scheduler_runs_levels_in_order() -> None:
    plan = _plan(_step("a"), _step("b", deps=("a",)), _step("c", deps=("b",))).validate()
    runner = _runner({"a": "A", "b": "B", "c": "C"})

    result = await run_plan(plan, runner)

    assert result.ok
    assert [key for key, _ in runner.calls] == ["a", "b", "c"]  # type: ignore[attr-defined]
    assert result.outcomes["c"].output == "C"
    assert result.sequential_levels == 3


@pytest.mark.asyncio
async def test_scheduler_runs_independent_steps_concurrently() -> None:
    started: list[str] = []
    gate = asyncio.Event()

    async def block(step: PlanStep, attempt: int) -> StepOutcome:
        started.append(step.key)
        if len(started) == 2:
            gate.set()
        await asyncio.wait_for(gate.wait(), timeout=1)
        return StepOutcome(ok=True, output=step.key)

    plan = _plan(_step("a"), _step("b")).validate()
    result = await run_plan(plan, block, max_parallel=2)

    assert result.ok
    assert sorted(started) == ["a", "b"]


@pytest.mark.asyncio
async def test_scheduler_skips_dependents_of_failures() -> None:
    plan = _plan(_step("a"), _step("b", deps=("a",)), _step("c")).validate()

    async def runner(step: PlanStep, attempt: int) -> StepOutcome:
        if step.key == "a":
            return StepOutcome(ok=False, error="nope", metadata={"retryable": False})
        return StepOutcome(ok=True, output=step.key)

    result = await run_plan(plan, runner)

    assert not result.ok
    assert result.failed == {"a"}
    assert result.skipped == {"b"}
    assert "c" in result.outcomes


@pytest.mark.asyncio
async def test_scheduler_retries_until_success() -> None:
    attempts: list[int] = []

    async def runner(step: PlanStep, attempt: int) -> StepOutcome:
        attempts.append(attempt)
        if attempt < 2:
            return StepOutcome(ok=False, error="flaky")
        return StepOutcome(ok=True, output="ok")

    plan = _plan(_step("a")).validate()
    result = await run_plan(plan, runner, max_attempts=3)

    assert result.ok
    assert attempts == [1, 2]
    assert result.outcomes["a"].attempts == 2


@pytest.mark.asyncio
async def test_scheduler_does_not_retry_non_retryable_failures() -> None:
    attempts: list[int] = []

    async def runner(step: PlanStep, attempt: int) -> StepOutcome:
        attempts.append(attempt)
        return StepOutcome(ok=False, error="fatal", metadata={"retryable": False})

    plan = _plan(_step("a")).validate()
    result = await run_plan(plan, runner, max_attempts=3)

    assert not result.ok
    assert attempts == [1]


@pytest.mark.asyncio
async def test_scheduler_survives_runner_exceptions() -> None:
    plan = _plan(_step("a")).validate()
    result = await run_plan(plan, _runner({"a": RuntimeError("kaboom")}), max_attempts=1)

    assert not result.ok
    assert "kaboom" in (result.outcomes["a"].error or "")


@pytest.mark.asyncio
async def test_scheduler_cancels_between_levels() -> None:
    plan = _plan(_step("a"), _step("b", deps=("a",))).validate()

    async def runner(step: PlanStep, attempt: int) -> StepOutcome:
        return StepOutcome(ok=True, output=step.key)

    calls = 0

    async def cancel_check() -> bool:
        nonlocal calls
        calls += 1
        return calls > 1

    result = await run_plan(plan, runner, cancel_check=cancel_check)

    assert result.cancelled
    assert "a" in result.outcomes
    assert "b" not in result.outcomes


@pytest.mark.asyncio
async def test_scheduler_reports_paused_steps() -> None:
    plan = _plan(_step("a"), _step("b")).validate()

    async def runner(step: PlanStep, attempt: int) -> StepOutcome:
        if step.key == "a":
            return StepOutcome(ok=False, paused=True, metadata={"retryable": False})
        return StepOutcome(ok=True, output="b")

    result = await run_plan(plan, runner)

    assert result.paused
    assert result.paused_step == "a"
    assert not result.ok


@pytest.mark.asyncio
async def test_scheduler_honours_deadline() -> None:
    plan = _plan(_step("a"), _step("b", deps=("a",))).validate()

    async def slow(step: PlanStep, attempt: int) -> StepOutcome:
        await asyncio.sleep(0.05)
        return StepOutcome(ok=True, output="x")

    result = await run_plan(plan, slow, timeout_seconds=0.01)

    assert result.timed_out


@pytest.mark.asyncio
async def test_aggregate_output_marks_statuses() -> None:
    plan = _plan(_step("a"), _step("b", deps=("a",))).validate()

    async def runner(step: PlanStep, attempt: int) -> StepOutcome:
        return StepOutcome(ok=True, output="fine")

    result = await run_plan(plan, runner)
    payload = aggregate_output(result, plan)

    steps = payload["steps"]
    assert isinstance(steps, list)
    assert {item["key"]: item["status"] for item in steps} == {
        "a": "completed",
        "b": "completed",
    }
    assert payload["summary"]["steps_ok"] == 2
