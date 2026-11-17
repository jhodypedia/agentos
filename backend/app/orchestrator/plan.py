"""Execution plans: a dependency graph of delegable steps.

A plan is provider- and storage-neutral. It validates itself (unique keys,
unknown/self/cyclic dependencies, agent assignment) and exposes the scheduling
information the orchestrator needs: topological levels for parallel execution
and a stable ordering for persistence.

Serialised plan steps map 1:1 onto ``task_steps`` rows, with the step ``key``
stored in ``meta`` so a resumed run can re-attach to its existing children.
"""

from __future__ import annotations

import re
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Literal

from app.core.exceptions import UnprocessableError

StepMode = Literal["agent", "orchestrator"]

_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
MAX_STEPS = 50
MAX_TITLE_CHARS = 300
MAX_INSTRUCTION_CHARS = 20_000


@dataclass(slots=True)
class PlanStep:
    """One delegable unit of work inside a plan."""

    key: str
    title: str
    instruction: str
    agent_slug: str | None = None
    mode: StepMode = "agent"
    depends_on: tuple[str, ...] = ()
    priority: int = 100
    max_iterations: int | None = None
    timeout_seconds: int | None = None
    budget_tokens: int | None = None
    budget_usd: str | None = None
    requires_approval: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def normalised(self) -> PlanStep:
        return PlanStep(
            key=self.key.strip().lower(),
            title=self.title.strip(),
            instruction=self.instruction.strip(),
            agent_slug=(self.agent_slug or "").strip().lower() or None,
            mode=self.mode,
            depends_on=tuple(dict.fromkeys(dep.strip().lower() for dep in self.depends_on if dep)),
            priority=self.priority,
            max_iterations=self.max_iterations,
            timeout_seconds=self.timeout_seconds,
            budget_tokens=self.budget_tokens,
            budget_usd=self.budget_usd,
            requires_approval=self.requires_approval,
            metadata=dict(self.metadata),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "instruction": self.instruction,
            "agent_slug": self.agent_slug,
            "mode": self.mode,
            "depends_on": list(self.depends_on),
            "priority": self.priority,
            "max_iterations": self.max_iterations,
            "timeout_seconds": self.timeout_seconds,
            "budget_tokens": self.budget_tokens,
            "budget_usd": self.budget_usd,
            "requires_approval": self.requires_approval,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> PlanStep:
        return cls(
            key=str(payload.get("key", "")),
            title=str(payload.get("title", "")),
            instruction=str(payload.get("instruction", "")),
            agent_slug=payload.get("agent_slug"),
            mode=payload.get("mode", "agent"),
            depends_on=tuple(payload.get("depends_on", ()) or ()),
            priority=int(payload.get("priority", 100)),
            max_iterations=payload.get("max_iterations"),
            timeout_seconds=payload.get("timeout_seconds"),
            budget_tokens=payload.get("budget_tokens"),
            budget_usd=payload.get("budget_usd"),
            requires_approval=bool(payload.get("requires_approval", False)),
            metadata=dict(payload.get("metadata", {}) or {}),
        )


@dataclass(slots=True)
class ExecutionPlan:
    """A validated DAG of plan steps."""

    objective: str
    steps: list[PlanStep]
    rationale: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def validate(self, *, known_agents: set[str] | None = None) -> ExecutionPlan:
        if not self.objective.strip():
            raise UnprocessableError("A plan needs a non-empty objective")
        if not self.steps:
            raise UnprocessableError("A plan needs at least one step")
        if len(self.steps) > MAX_STEPS:
            raise UnprocessableError(f"A plan may contain at most {MAX_STEPS} steps")

        normalised = [step.normalised() for step in self.steps]
        keys: set[str] = set()
        for step in normalised:
            if not _KEY_PATTERN.match(step.key):
                raise UnprocessableError(
                    f"Step key '{step.key}' must match [a-z][a-z0-9_-]{{0,63}}"
                )
            if step.key in keys:
                raise UnprocessableError(f"Duplicate step key '{step.key}'")
            keys.add(step.key)
            if not step.title:
                raise UnprocessableError(f"Step '{step.key}' needs a title")
            if len(step.title) > MAX_TITLE_CHARS:
                raise UnprocessableError(f"Step '{step.key}' title is too long")
            if not step.instruction:
                raise UnprocessableError(f"Step '{step.key}' needs an instruction")
            if len(step.instruction) > MAX_INSTRUCTION_CHARS:
                raise UnprocessableError(f"Step '{step.key}' instruction is too long")
            if step.mode not in {"agent", "orchestrator"}:
                raise UnprocessableError(f"Step '{step.key}' has an unknown mode")
            if step.mode == "agent" and not step.agent_slug:
                raise UnprocessableError(f"Step '{step.key}' needs an agent_slug")
            if (
                known_agents is not None
                and step.agent_slug is not None
                and step.agent_slug not in known_agents
            ):
                raise UnprocessableError(
                    f"Step '{step.key}' references unknown agent '{step.agent_slug}'"
                )

        for step in normalised:
            for dependency in step.depends_on:
                if dependency == step.key:
                    raise UnprocessableError(f"Step '{step.key}' cannot depend on itself")
                if dependency not in keys:
                    raise UnprocessableError(
                        f"Step '{step.key}' depends on unknown step '{dependency}'"
                    )

        self.steps = normalised
        self.levels()  # raises on cycles
        return self

    def by_key(self) -> dict[str, PlanStep]:
        return {step.key: step for step in self.steps}

    def levels(self) -> list[list[PlanStep]]:
        """Group steps into topologically ordered execution levels.

        Every step in a level only depends on steps in earlier levels, so a level
        can be executed in parallel. Raises :class:`UnprocessableError` on a cycle.
        """
        plan_by_key = self.by_key()
        indegree = dict.fromkeys(plan_by_key, 0)
        dependents: dict[str, list[str]] = defaultdict(list)
        for step in self.steps:
            for dependency in step.depends_on:
                indegree[step.key] += 1
                dependents[dependency].append(step.key)

        ready = deque(sorted(key for key, degree in indegree.items() if degree == 0))
        ordered: list[str] = []
        while ready:
            key = ready.popleft()
            ordered.append(key)
            for dependent in sorted(dependents[key]):
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    ready.append(dependent)

        if len(ordered) != len(plan_by_key):
            cyclic = sorted(key for key, degree in indegree.items() if degree > 0)
            raise UnprocessableError(
                "The plan contains a dependency cycle", details={"steps": cyclic}
            )

        depth: dict[str, int] = {}
        for key in ordered:
            step = plan_by_key[key]
            depth[key] = 1 + max((depth[dep] for dep in step.depends_on), default=-1)

        levels: dict[int, list[PlanStep]] = defaultdict(list)
        for key in ordered:
            levels[depth[key]].append(plan_by_key[key])
        return [
            sorted(levels[level], key=lambda step: (step.priority, step.key))
            for level in sorted(levels)
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "objective": self.objective,
            "rationale": self.rationale,
            "metadata": self.metadata,
            "steps": [step.to_dict() for step in self.steps],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ExecutionPlan:
        raw_steps = payload.get("steps", [])
        if not isinstance(raw_steps, list):
            raise UnprocessableError("'steps' must be a list")
        steps = [PlanStep.from_dict(item) for item in raw_steps if isinstance(item, dict)]
        return cls(
            objective=str(payload.get("objective", "")),
            steps=steps,
            rationale=str(payload.get("rationale", "")),
            metadata=dict(payload.get("metadata", {}) or {}),
        )
