"""DAG scheduler: run plan steps respecting dependencies, in parallel levels.

The scheduler owns *control flow* only. Executing a step is delegated to a
``StepRunner`` callable, which decides whether it means "call an agent" or
"run the orchestrator's own LLM turn". Persistence, events and budgets are
likewise the runner's concern; the scheduler enforces graph order, retries,
cancellation and a global deadline.

Parallelism is bounded (``max_parallel``) and a failing step never cancels its
siblings: independent branches finish, dependents of the failure are skipped.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

import structlog

from app.orchestrator.plan import ExecutionPlan, PlanStep

logger = structlog.get_logger("agentos.orchestrator.scheduler")

CancelCheck = Callable[[], Awaitable[bool]]


@dataclass(slots=True)
class StepOutcome:
    """What a step runner reports back to the scheduler."""

    ok: bool
    output: str = ""
    error: str | None = None
    skipped: bool = False
    paused: bool = False
    attempts: int = 1
    tokens: int = 0
    cost_usd: str = "0"
    metadata: dict[str, object] = field(default_factory=dict)


class StepRunner(Protocol):
    async def __call__(self, step: PlanStep, attempt: int) -> StepOutcome: ...


@dataclass(slots=True)
class ScheduleResult:
    """The aggregate outcome of scheduling one plan."""

    outcomes: dict[str, StepOutcome] = field(default_factory=dict)
    skipped: set[str] = field(default_factory=set)
    failed: set[str] = field(default_factory=set)
    cancelled: bool = False
    timed_out: bool = False
    paused: bool = False
    paused_step: str | None = None
    sequential_levels: int = 0

    @property
    def ok(self) -> bool:
        return not self.failed and not self.cancelled and not self.timed_out and not self.paused

    @property
    def total_tokens(self) -> int:
        return sum(outcome.tokens for outcome in self.outcomes.values())

    def summary(self) -> dict[str, object]:
        return {
            "steps_total": len(self.outcomes) + len(self.skipped),
            "steps_ok": sum(1 for outcome in self.outcomes.values() if outcome.ok),
            "steps_failed": sorted(self.failed),
            "steps_skipped": sorted(self.skipped),
            "cancelled": self.cancelled,
            "timed_out": self.timed_out,
            "paused": self.paused,
            "paused_step": self.paused_step,
            "levels": self.sequential_levels,
            "tokens": self.total_tokens,
        }


def _blocked_by(step: PlanStep, failed: set[str], skipped: set[str]) -> bool:
    return any(dependency in failed or dependency in skipped for dependency in step.depends_on)


async def run_plan(
    plan: ExecutionPlan,
    runner: StepRunner,
    *,
    max_parallel: int = 3,
    max_attempts: int = 2,
    timeout_seconds: float | None = None,
    cancel_check: CancelCheck | None = None,
    retry_delay: float = 0.0,
) -> ScheduleResult:
    """Execute ``plan`` level by level, returning per-step outcomes."""
    if max_parallel < 1:
        raise ValueError("max_parallel must be at least 1")
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")

    result = ScheduleResult()
    semaphore = asyncio.Semaphore(max_parallel)
    deadline = time.monotonic() + timeout_seconds if timeout_seconds else None
    levels = plan.levels()
    result.sequential_levels = len(levels)

    for level_index, level in enumerate(levels, start=1):
        if cancel_check is not None and await cancel_check():
            result.cancelled = True
            logger.info("plan_cancelled", level=level_index)
            break
        if deadline is not None and time.monotonic() > deadline:
            result.timed_out = True
            logger.warning("plan_deadline_reached", level=level_index)
            break

        runnable: list[PlanStep] = []
        for step in level:
            if _blocked_by(step, result.failed, result.skipped):
                result.skipped.add(step.key)
                continue
            runnable.append(step)

        async def _execute(step: PlanStep) -> tuple[str, StepOutcome]:
            async with semaphore:
                return (
                    step.key,
                    await _run_with_retries(
                        step,
                        runner,
                        max_attempts=max_attempts,
                        retry_delay=retry_delay,
                        deadline=deadline,
                        cancel_check=cancel_check,
                    ),
                )

        completed = await asyncio.gather(*(_execute(step) for step in runnable))
        for key, outcome in completed:
            if outcome.skipped:
                result.skipped.add(key)
                continue
            if outcome.paused:
                result.paused = True
                result.paused_step = key
                logger.info("plan_paused", step=key, level=level_index)
                continue
            result.outcomes[key] = outcome
            if not outcome.ok:
                result.failed.add(key)
                if outcome.metadata.get("cancelled"):
                    result.cancelled = True
                if outcome.metadata.get("deadline"):
                    result.timed_out = True
            logger.info(
                "step_finished",
                step=key,
                ok=outcome.ok,
                attempts=outcome.attempts,
                level=level_index,
            )

        if result.cancelled or result.timed_out or result.paused:
            break

    return result


async def _run_with_retries(
    step: PlanStep,
    runner: StepRunner,
    *,
    max_attempts: int,
    retry_delay: float,
    deadline: float | None,
    cancel_check: CancelCheck | None,
) -> StepOutcome:
    last: StepOutcome | None = None
    for attempt in range(1, max_attempts + 1):
        if cancel_check is not None and await cancel_check():
            return StepOutcome(
                ok=False, error="Cancelled before the step started", metadata={"cancelled": True}
            )
        if deadline is not None and time.monotonic() > deadline:
            return StepOutcome(
                ok=False,
                error="Deadline reached before the step started",
                metadata={"deadline": True},
            )
        try:
            outcome = await runner(step, attempt)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # the runner must never take the scheduler down
            logger.exception("step_runner_failed", step=step.key, attempt=attempt)
            outcome = StepOutcome(ok=False, error=f"{type(exc).__name__}: {exc}")

        outcome.attempts = attempt
        if outcome.ok:
            return outcome
        last = outcome
        retryable = bool(outcome.metadata.get("retryable", True))
        if outcome.metadata.get("cancelled") or outcome.metadata.get("deadline"):
            return outcome
        if not retryable or attempt >= max_attempts:
            break
        if retry_delay > 0:
            await asyncio.sleep(retry_delay * attempt)

    return last or StepOutcome(ok=False, error="The step produced no outcome")


def aggregate_output(result: ScheduleResult, plan: ExecutionPlan) -> dict[str, object]:
    """Build the persisted task output from a schedule result."""
    errors = {
        key: outcome.error for key, outcome in result.outcomes.items() if outcome.error is not None
    }
    return {
        "steps": [
            {
                "key": step.key,
                "title": step.title,
                "agent_slug": step.agent_slug,
                "status": (
                    "skipped"
                    if step.key in result.skipped
                    else "completed"
                    if result.outcomes.get(step.key, StepOutcome(ok=False)).ok
                    else "failed"
                ),
                "attempts": result.outcomes.get(
                    step.key, StepOutcome(ok=True, attempts=0)
                ).attempts,
                "output": (result.outcomes.get(step.key, StepOutcome(ok=True)).output or "")[
                    :4_000
                ],
                "error": result.outcomes.get(step.key, StepOutcome(ok=True)).error,
            }
            for step in plan.steps
        ],
        "summary": result.summary(),
        "errors": errors,
    }


def ordered_outputs(result: ScheduleResult, plan: ExecutionPlan) -> Sequence[tuple[str, str]]:
    """Step outputs in plan order, for report assembly."""
    return [
        (step.key, result.outcomes[step.key].output)
        for step in plan.steps
        if step.key in result.outcomes and result.outcomes[step.key].ok
    ]
