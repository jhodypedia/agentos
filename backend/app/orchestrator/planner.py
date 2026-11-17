"""Planning: turn a task objective into a validated execution plan.

Two strategies:

* ``LlmPlanner`` asks a model for a structured plan (JSON), validates it and
  falls back to a deterministic single-step plan when the model output is
  unusable. Model failures never abort a task — they degrade the plan.
* ``single_step_plan`` is the deterministic fallback: one step assigned to the
  task's own agent, which is exactly Phase 2 behaviour.

The planner never executes anything and never touches the database.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Protocol

import structlog

from app.core.exceptions import UnprocessableError
from app.orchestrator.plan import ExecutionPlan, PlanStep
from app.providers.base import ChatMessage, LLMRequest, LLMResponse, ToolSpec
from app.providers.errors import ProviderError

logger = structlog.get_logger("agentos.orchestrator.planner")

MAX_CONTEXT_CHARS = 8_000

PLAN_TOOL = ToolSpec(
    name="submit_plan",
    description=(
        "Submit the execution plan. Each step is delegated to one specialist agent "
        "and may depend on earlier steps."
    ),
    parameters={
        "type": "object",
        "properties": {
            "rationale": {
                "type": "string",
                "description": "One or two sentences explaining the decomposition.",
            },
            "steps": {
                "type": "array",
                "minItems": 1,
                "maxItems": 20,
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {
                            "type": "string",
                            "pattern": "^[a-z][a-z0-9_-]{0,63}$",
                            "description": "Short unique identifier, e.g. 'scan-repo'.",
                        },
                        "title": {"type": "string", "description": "Human-readable title."},
                        "instruction": {
                            "type": "string",
                            "description": "Self-contained instruction for the specialist.",
                        },
                        "agent_slug": {
                            "type": "string",
                            "description": "Slug of the specialist agent that runs this step.",
                        },
                        "depends_on": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Keys of steps that must finish first.",
                        },
                    },
                    "required": ["key", "title", "instruction", "agent_slug"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["steps"],
        "additionalProperties": False,
    },
)

PLANNER_SYSTEM_PROMPT = """You are the planning stage of an autonomous agent platform.

Break the objective into a small dependency graph of steps that specialist agents
can execute independently. Rules:

- Use 1-6 steps. Prefer fewer, well-scoped steps over many tiny ones.
- Every step must be self-contained: the specialist sees only its instruction,
  the objective and the outputs of the steps it depends on.
- Assign each step an `agent_slug` from the available agents listed by the user.
- Dependencies must form a DAG: no cycles, no self references, no forward
  references that cannot resolve.
- Only add a dependency when the step genuinely needs the other step's output.
- Steps with no dependency run in parallel, so keep independent work separate.
- Call `submit_plan` exactly once with the finished plan.
"""


class Planner(Protocol):
    async def plan(self, *, objective: str, context: str | None = None) -> ExecutionPlan: ...


def single_step_plan(
    *,
    objective: str,
    agent_slug: str,
    title: str | None = None,
    rationale: str = "Deterministic single-step plan.",
) -> ExecutionPlan:
    """Fallback plan: one step, executed by the normally assigned agent."""
    clean = " ".join(objective.split())
    step = PlanStep(
        key="execute",
        title=(title or clean[:200] or "Execute objective").strip(),
        instruction=objective.strip(),
        agent_slug=agent_slug,
    )
    return ExecutionPlan(
        objective=objective,
        steps=[step],
        rationale=rationale,
        metadata={"strategy": "single_step"},
    ).validate()


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _extract_json(text: str) -> dict[str, Any] | None:
    """Pull a JSON object out of a model response, tolerating fences and prose."""
    candidates: list[str] = []
    stripped = text.strip()
    if stripped:
        candidates.append(stripped)
    candidates.extend(match.strip() for match in _FENCE.findall(text))

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def plan_from_response(response: LLMResponse) -> ExecutionPlan | None:
    """Convert an LLM response into a plan, preferring a ``submit_plan`` call."""
    payload: dict[str, Any] | None = None
    for call in response.tool_calls:
        if call.name == PLAN_TOOL.name and isinstance(call.arguments, dict):
            payload = call.arguments
            break
    if payload is None:
        payload = _extract_json(response.content)
    if payload is None:
        return None
    return ExecutionPlan.from_dict(payload)


@dataclass(slots=True)
class LlmPlanner:
    """Ask a model for a plan, with a deterministic fallback."""

    provider: Any
    model: str
    agent_slug: str
    available_agents: dict[str, str]
    temperature: float = 0.1
    max_output_tokens: int = 2_000
    timeout_seconds: float = 120.0
    step_max_iterations: int = 30

    def _user_prompt(self, objective: str, context: str | None) -> str:
        catalogue = "\n".join(
            f"- {slug}: {description or 'no description'}"
            for slug, description in sorted(self.available_agents.items())
        )
        parts = [
            f"Objective:\n{objective.strip()}",
            f"Available agents:\n{catalogue}",
        ]
        if context:
            trimmed = context.strip()[:MAX_CONTEXT_CHARS]
            parts.append(f"Additional context:\n{trimmed}")
        parts.append("Produce the plan with submit_plan.")
        return "\n\n".join(parts)

    async def plan(self, *, objective: str, context: str | None = None) -> ExecutionPlan:
        request = LLMRequest(
            model=self.model,
            messages=[
                ChatMessage(role="system", content=PLANNER_SYSTEM_PROMPT),
                ChatMessage(role="user", content=self._user_prompt(objective, context)),
            ],
            tools=[PLAN_TOOL],
            temperature=self.temperature,
            max_output_tokens=self.max_output_tokens,
            timeout_seconds=self.timeout_seconds,
        )

        try:
            response = await self.provider.complete(request)
        except ProviderError as exc:
            logger.warning("planner_provider_error", error=str(exc))
            return self._fallback(objective, reason="provider_error")

        try:
            plan = plan_from_response(response)
        except UnprocessableError as exc:
            logger.warning("planner_invalid_plan", error=exc.message)
            return self._fallback(objective, reason="invalid_plan")

        if plan is None:
            logger.warning("planner_unparseable_response")
            return self._fallback(objective, reason="unparseable_response")

        plan.objective = plan.objective or objective
        plan.metadata.setdefault("strategy", "llm")
        plan.metadata["planner_model"] = self.model
        for step in plan.steps:
            if step.max_iterations is None:
                step.max_iterations = self.step_max_iterations

        try:
            return plan.validate(known_agents=set(self.available_agents))
        except UnprocessableError as exc:
            logger.warning("planner_validation_failed", error=exc.message)
            return self._fallback(objective, reason="validation_failed")

    def _fallback(self, objective: str, *, reason: str) -> ExecutionPlan:
        plan = single_step_plan(
            objective=objective,
            agent_slug=self.agent_slug,
            rationale=f"Single-step plan ({reason.replace('_', ' ')}).",
        )
        plan.metadata["fallback_reason"] = reason
        return plan


def render_dependency_context(
    step: PlanStep, outputs: dict[str, str], *, max_chars: int = 6_000
) -> str:
    """Render the outputs of a step's dependencies for inclusion in its prompt."""
    if not step.depends_on:
        return ""
    blocks: list[str] = []
    for dependency in step.depends_on:
        content = (outputs.get(dependency) or "").strip()
        if not content:
            blocks.append(f"### Output of '{dependency}'\n(no output produced)")
            continue
        blocks.append(f"### Output of '{dependency}'\n{content}")
    rendered = "\n\n".join(blocks)
    if len(rendered) > max_chars:
        rendered = rendered[:max_chars] + "\n…(truncated)"
    return rendered
