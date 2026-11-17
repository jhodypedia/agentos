"""Bounty and competition API schemas."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

_SHA = r"^[0-9a-f]{40}$"


class BountyProgramPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    slug: str
    name: str
    platform: str
    url: str
    description: str
    created_at: datetime


class CompetitionPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    program_id: UUID
    project_id: UUID | None
    external_id: str
    title: str
    description: str
    url: str
    reward_amount: Decimal | None
    reward_currency: str
    status: str
    starts_at: datetime | None
    deadline_at: datetime | None
    submitted_at: datetime | None
    scoring: dict[str, Any]
    meta: dict[str, Any]
    created_at: datetime


class CompetitionDetail(CompetitionPublic):
    requirements_summary: dict[str, Any] = Field(default_factory=dict)


class CompetitionSyncRequest(BaseModel):
    limit: int | None = Field(default=None, ge=1, le=100)
    status: str | None = Field(default=None, pattern=r"^(draft|open|closed|resolved)$")


class CompetitionSyncResult(BaseModel):
    created: list[str]
    updated: list[str]
    total: int
    next_cursor: str | None = None
    synced_at: datetime


class EntryPrepare(BaseModel):
    repo_url: str = Field(min_length=1, max_length=1000)
    commit_sha: str = Field(min_length=40, max_length=40, pattern=_SHA)
    title: str = Field(default="", max_length=200)
    transcript_paths: list[str] = Field(default_factory=list, max_length=32)


class CompetitionEntryPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    competition_id: UUID
    project_id: UUID | None
    approval_id: UUID | None
    status: str
    submission_id: str | None
    title: str
    repo_url: str
    commit_sha: str
    transcript_paths: list[str]
    checks: list[dict[str, Any]]
    score: str | None
    rank: int | None
    prize_usd: Decimal | None
    error: str | None
    submitted_at: datetime | None
    last_synced_at: datetime | None
    created_at: datetime


class QuotaPublic(BaseModel):
    model_config = ConfigDict(extra="allow")

    max_per_day: int | None = None
    max_entries_per_account: int | None = None
