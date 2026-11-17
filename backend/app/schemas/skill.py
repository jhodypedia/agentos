"""Skill API schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class SkillPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    slug: str
    name: str
    description: str
    version: str
    source: str
    enabled: bool
    is_system: bool
    content_hash: str
    frontmatter: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class SkillDetail(SkillPublic):
    body: str = ""
    files: list[str] = Field(default_factory=list)
    assigned_agent_ids: list[UUID] = Field(default_factory=list)


class SkillCreate(BaseModel):
    slug: str = Field(min_length=1, max_length=120, pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=2000)
    version: str = Field(default="1.0.0", max_length=32)
    body: str = Field(min_length=1)
    frontmatter: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class SkillUpdate(BaseModel):
    enabled: bool | None = None
    description: str | None = Field(default=None, max_length=2000)


class SkillImportRequest(BaseModel):
    repo_url: str = Field(min_length=1, max_length=500)
    ref: str | None = Field(default=None, max_length=200)
    subdirectory: str | None = Field(default=None, max_length=300)
    overwrite: bool = True


class SkillSyncResponse(BaseModel):
    installed: list[str] = Field(default_factory=list)
    updated: list[str] = Field(default_factory=list)
    unchanged: list[str] = Field(default_factory=list)
    failed: dict[str, str] = Field(default_factory=dict)


class SkillValidationRequest(BaseModel):
    """Validate either a path under the skills root or raw SKILL.md content."""

    path: str | None = Field(default=None, max_length=500)
    content: str | None = Field(default=None, max_length=300_000)
    slug: str | None = Field(default=None, max_length=120)

    @property
    def has_input(self) -> bool:
        return bool(self.path or self.content)


class SkillValidationResponse(BaseModel):
    valid: bool
    slug: str | None = None
    version: str | None = None
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class SkillExecutionPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    skill_id: UUID
    agent_id: UUID | None
    task_id: UUID | None
    event: str
    ok: bool
    detail: str
    duration_ms: int
    meta: dict[str, Any]
    created_at: datetime
