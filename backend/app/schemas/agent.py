"""Agent request/response schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.db.enums import AgentStatus, RiskLevel


class AgentCreate(BaseModel):
    slug: str = Field(min_length=2, max_length=80, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=4000)
    system_prompt: str = Field(default="", max_length=100_000)
    model_provider_id: UUID | None = None
    model_name: str | None = Field(default=None, max_length=160)
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    top_p: float = Field(default=1.0, gt=0.0, le=1.0)
    max_output_tokens: int = Field(default=4096, ge=1, le=100_000)
    max_iterations: int = Field(default=25, ge=1, le=200)
    token_budget: int = Field(default=200_000, ge=1, le=20_000_000)
    timeout_seconds: int = Field(default=1800, ge=1, le=86_400)
    permissions: dict[str, Any] = Field(default_factory=lambda: {"tools": []})
    memory_config: dict[str, Any] = Field(default_factory=dict)
    status: AgentStatus = AgentStatus.ACTIVE


class AgentUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=4000)
    system_prompt: str | None = Field(default=None, max_length=100_000)
    model_provider_id: UUID | None = None
    model_name: str | None = Field(default=None, max_length=160)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    top_p: float | None = Field(default=None, gt=0.0, le=1.0)
    max_output_tokens: int | None = Field(default=None, ge=1, le=100_000)
    max_iterations: int | None = Field(default=None, ge=1, le=200)
    token_budget: int | None = Field(default=None, ge=1, le=20_000_000)
    timeout_seconds: int | None = Field(default=None, ge=1, le=86_400)
    permissions: dict[str, Any] | None = None
    memory_config: dict[str, Any] | None = None
    status: AgentStatus | None = None


class AgentToolPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tool_id: UUID
    enabled: bool
    config: dict[str, Any]


class AgentSkillPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    skill_id: UUID
    enabled: bool
    config: dict[str, Any]


class AgentPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    slug: str
    name: str
    description: str
    system_prompt: str
    model_provider_id: UUID | None
    model_name: str | None
    temperature: float
    top_p: float
    max_output_tokens: int
    max_iterations: int
    token_budget: int
    timeout_seconds: int
    permissions: dict[str, Any]
    memory_config: dict[str, Any]
    status: AgentStatus
    is_system: bool
    version: int
    created_at: datetime
    updated_at: datetime


class AgentDetail(AgentPublic):
    tools: list[AgentToolPublic] = Field(default_factory=list)
    skills: list[AgentSkillPublic] = Field(default_factory=list)


class AgentToolAssign(BaseModel):
    tool_slug: str = Field(min_length=1, max_length=80)
    enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)


class AgentSkillAssign(BaseModel):
    skill_id: UUID
    enabled: bool = True


class ToolPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    slug: str
    name: str
    description: str
    input_schema: dict[str, Any]
    required_permissions: list[str]
    risk_level: RiskLevel
    timeout_seconds: int
    execution_environment: str
    enabled: bool
    version: str


class ToolExecutionRequest(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolExecutionResponse(BaseModel):
    ok: bool
    output: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    duration_ms: int
