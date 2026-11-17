"""Approval and agent-message schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.db.enums import ApprovalAction, ApprovalStatus


class ApprovalCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=20_000)
    action_type: ApprovalAction = ApprovalAction.OTHER
    task_id: UUID | None = None
    step_id: UUID | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    ttl_hours: int = Field(default=72, ge=1, le=720)


class ApprovalDecision(BaseModel):
    approved: bool
    note: str = Field(default="", max_length=5_000)


class ApprovalPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    task_id: UUID | None
    step_id: UUID | None
    finding_id: UUID | None
    requested_by_agent_id: UUID | None
    assigned_to_user_id: UUID | None
    decided_by_user_id: UUID | None
    action_type: ApprovalAction
    title: str
    description: str
    payload: dict[str, Any]
    status: ApprovalStatus
    decision_note: str
    expires_at: datetime | None
    decided_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ApprovalPage(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[ApprovalPublic]


class AgentMessagePublic(BaseModel):
    id: str
    type: str
    from_agent: str | None = Field(default=None, alias="from")
    to_agent: str | None = Field(default=None, alias="to")
    content: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime

    model_config = ConfigDict(populate_by_name=True)
