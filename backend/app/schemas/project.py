"""Project and workspace API schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

_SLUG = r"^[a-z0-9][a-z0-9-]{0,62}$"


class ProjectPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    slug: str
    name: str
    description: str
    owner_id: UUID | None
    workspace_path: str
    status: str
    meta: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class ProjectCreate(BaseModel):
    slug: str = Field(min_length=1, max_length=63, pattern=_SLUG)
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)


class WorkspaceFile(BaseModel):
    path: str
    type: str
    size: int


class WorkspaceListing(BaseModel):
    project_id: UUID
    slug: str
    path: str
    exists: bool
    file_count: int
    total_bytes: int
    repositories: int


class WorkspaceTree(BaseModel):
    project_id: UUID
    path: str
    entries: list[WorkspaceFile]
    count: int


class FileContent(BaseModel):
    project_id: UUID
    path: str
    bytes: int
    lines: int
    content: str


class FileWriteRequest(BaseModel):
    path: str = Field(min_length=1, max_length=1000)
    content: str = Field(max_length=1_000_000)


class FileWriteResponse(BaseModel):
    project_id: UUID
    path: str
    bytes: int
    created: bool


class CommandRequest(BaseModel):
    command: str = Field(min_length=1, max_length=4000)
    timeout_seconds: int = Field(default=60, ge=1, le=600)


class CommandResponse(BaseModel):
    project_id: UUID
    command: str
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool
    truncated: bool
    sandbox: str
    ok: bool


class RepositoryCreate(BaseModel):
    url: str = Field(min_length=8, max_length=1000)
    name: str | None = Field(default=None, max_length=200)
    branch: str | None = Field(default=None, max_length=200)


class RepositoryPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    name: str
    url: str
    provider: str
    default_branch: str
    local_path: str | None
    last_analyzed_at: datetime | None


class SearchRequest(BaseModel):
    pattern: str = Field(min_length=1, max_length=500)
    glob: str = Field(default="**/*", max_length=200)
    max_results: int = Field(default=50, ge=1, le=500)


class SearchMatch(BaseModel):
    path: str
    line: int
    text: str


class SearchResponse(BaseModel):
    project_id: UUID
    pattern: str
    matches: list[SearchMatch]
    count: int


class CodingEngineInfo(BaseModel):
    name: str
    kind: str
    capabilities: list[str]
    available: bool
    default: bool
    binary: str | None = None
    agent: str | None = None


class CodingRunRequest(BaseModel):
    instruction: str = Field(min_length=1, max_length=20_000)
    engine: str | None = Field(default=None, max_length=64)
    model: str | None = Field(default=None, max_length=200)
    session_id: str | None = Field(default=None, max_length=200)
    timeout_seconds: int = Field(default=300, ge=10, le=1800)


class CodingChange(BaseModel):
    status: str
    path: str


class CodingRunResponse(BaseModel):
    project_id: UUID
    engine: str
    ok: bool
    session_id: str | None
    summary: str
    changes: list[CodingChange]
    commands: list[dict[str, Any]]
    transcript_path: str | None
    duration_ms: int
    exit_code: int
    error: str | None


class CodingSessionPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    task_id: UUID | None
    engine: str
    model: str | None
    session_id: str | None
    status: str
    instruction: str
    summary: str
    transcript_path: str | None
    exit_code: int
    duration_ms: int
    changes: list[Any]
    error: str | None
    created_at: datetime
    finished_at: datetime | None
