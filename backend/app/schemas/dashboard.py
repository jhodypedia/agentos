"""Dashboard aggregate, usage and activity schemas."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ActivityItem(BaseModel):
    id: UUID
    kind: str
    action: str
    resource_type: str
    resource_id: str | None = None
    success: bool = True
    actor_user_id: UUID | None = None
    created_at: datetime


class UsagePoint(BaseModel):
    day: str
    requests: int
    tokens: int
    cost_usd: Decimal


class UsageSeries(BaseModel):
    days: int
    points: list[UsagePoint] = Field(default_factory=list)
    total_requests: int = 0
    total_tokens: int = 0
    total_cost_usd: Decimal = Decimal("0")


class DashboardSummary(BaseModel):
    generated_at: datetime
    health: dict[str, str]
    agents_by_status: dict[str, int] = Field(default_factory=dict)
    tasks_by_status: dict[str, int] = Field(default_factory=dict)
    agents_total: int = 0
    tasks_total: int = 0
    approvals_pending: int = 0
    open_competitions: int = 0
    findings_by_severity: dict[str, int] = Field(default_factory=dict)
    total_tokens: int = 0
    total_cost_usd: Decimal = Decimal("0")
    activity: list[ActivityItem] = Field(default_factory=list)
    limits: dict[str, Any] = Field(default_factory=dict)


class EventPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    type: str
    occurred_at: datetime
    source: str
    task_id: UUID | None = None
    agent_id: UUID | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class AuditPage(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[ActivityItem] = Field(default_factory=list)
