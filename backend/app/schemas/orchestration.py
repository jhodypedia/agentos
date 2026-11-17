"""Orchestration request/response schemas."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class PlanStepPublic(BaseModel):
    key: str
    title: str
    instruction: str
    agent_slug: str | None = None
    mode: Literal["agent", "orchestrator"] = "agent"
    depends_on: list[str] = Field(default_factory=list)
    priority: int = 100


class PlanPublic(BaseModel):
    objective: str
    rationale: str = ""
    steps: list[PlanStepPublic] = Field(default_factory=list)


class PlanRequest(BaseModel):
    objective: str = Field(min_length=1, max_length=20_000)
    context: str | None = Field(default=None, max_length=20_000)
    max_steps: int = Field(default=8, ge=1, le=12)


class OrchestrationOptionsRequest(BaseModel):
    max_parallel: int = Field(default=3, ge=1, le=10)
    max_step_attempts: int = Field(default=2, ge=1, le=5)
    use_llm_planner: bool = True
    verify: bool = False
    verifier_slug: str = Field(default="independent-reviewer", max_length=80)


class OrchestrateRequest(OrchestrationOptionsRequest):
    plan: PlanPublic | None = None


class StepResultPublic(BaseModel):
    key: str
    title: str
    agent_slug: str | None
    depends_on: list[str] = Field(default_factory=list)
    status: str
    attempts: int = 0
    tokens: int = 0
    cost_usd: str = "0"
    output: str = ""
    error: str | None = None


class OrchestrationResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    task_id: str
    status: str
    content: str = ""
    error: str | None = None
    plan: PlanPublic | None = None
    steps: list[StepResultPublic] = Field(default_factory=list)
    children: dict[str, str] = Field(default_factory=dict)
    totals: dict[str, Any] = Field(default_factory=dict)
    summary: dict[str, Any] = Field(default_factory=dict)
    needs_approval: bool = False
