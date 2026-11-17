"""Memory API schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.db.enums import MemoryLayer


class MemoryPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    layer: MemoryLayer
    key: str | None
    content: str
    summary: str | None
    agent_id: UUID | None
    project_id: UUID | None
    task_id: UUID | None
    user_id: UUID | None
    parent_memory_id: UUID | None
    source: str
    source_ref: str | None
    importance: float
    confidence: float
    version: int
    embedding_model: str | None
    access_count: int
    accessed_at: datetime | None
    expires_at: datetime | None
    meta: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class MemoryCreate(BaseModel):
    content: str = Field(min_length=1, max_length=8000)
    layer: MemoryLayer = MemoryLayer.SEMANTIC
    key: str | None = Field(default=None, max_length=200)
    summary: str | None = Field(default=None, max_length=2000)
    agent_id: UUID | None = None
    project_id: UUID | None = None
    task_id: UUID | None = None
    importance: float = Field(default=0.5, ge=0, le=1)
    confidence: float = Field(default=0.5, ge=0, le=1)
    ttl_days: int | None = Field(default=None, ge=1, le=3650)
    meta: dict[str, Any] = Field(default_factory=dict)


class MemoryUpdate(BaseModel):
    content: str | None = Field(default=None, min_length=1, max_length=8000)
    summary: str | None = Field(default=None, max_length=2000)
    importance: float | None = Field(default=None, ge=0, le=1)
    confidence: float | None = Field(default=None, ge=0, le=1)
    ttl_days: int | None = Field(default=None, ge=1, le=3650)


class MemoryScore(BaseModel):
    score: float
    lexical: float
    recency: float
    usage: float
    vector: float | None = None


class MemorySearchHit(BaseModel):
    memory: MemoryPublic
    ranking: MemoryScore


class MemorySearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    agent_id: UUID | None = None
    project_id: UUID | None = None
    task_id: UUID | None = None
    layers: list[MemoryLayer] | None = None
    limit: int = Field(default=10, ge=1, le=100)
    use_vector: bool = True


class MemoryContextResponse(BaseModel):
    query: str
    context: str
    items: int


class MemorySummaryResponse(BaseModel):
    summary: str
    items: int
