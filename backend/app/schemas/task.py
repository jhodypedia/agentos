"""Task request/response schemas."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.db.enums import EventLevel, StepKind, StepStatus, TaskStatus


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    objective: str = Field(min_length=1, max_length=20_000)
    description: str = Field(default="", max_length=20_000)
    assigned_agent_id: UUID | None = None
    project_id: UUID | None = None
    input: dict[str, Any] = Field(default_factory=dict)
    budget_tokens: int | None = Field(default=None, ge=1)
    budget_usd: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=6)
    max_iterations: int | None = Field(default=None, ge=1, le=200)
    timeout_seconds: int | None = Field(default=None, ge=1, le=86_400)
    deadline_at: datetime | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class TaskStepPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    seq: int
    kind: StepKind
    status: StepStatus
    title: str
    description: str
    agent_id: UUID | None
    attempt: int
    error: str | None
    started_at: datetime | None
    completed_at: datetime | None


class TaskEventPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    event_type: str
    level: EventLevel
    message: str
    payload: dict[str, Any]
    created_at: datetime


class TaskPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    objective: str
    description: str
    status: TaskStatus
    priority: int
    assigned_agent_id: UUID | None
    project_id: UUID | None
    created_by: UUID | None
    budget_tokens: int | None
    used_tokens: int
    budget_usd: Decimal | None
    cost_usd: Decimal
    max_iterations: int
    used_iterations: int
    timeout_seconds: int
    output: dict[str, Any]
    error: str | None
    started_at: datetime | None
    completed_at: datetime | None
    deadline_at: datetime | None
    created_at: datetime
    updated_at: datetime


class TaskDetail(TaskPublic):
    steps: list[TaskStepPublic] = Field(default_factory=list)
    events: list[TaskEventPublic] = Field(default_factory=list)


class TaskPage(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[TaskPublic]


class TaskRunResponse(BaseModel):
    task_id: UUID
    status: TaskStatus
    content: str
    iterations: int
    total_tokens: int
    cost_usd: Decimal
    error: str | None = None
    tool_invocations: list[dict[str, Any]] = Field(default_factory=list)
    pending_approvals: list[dict[str, Any]] = Field(default_factory=list)
