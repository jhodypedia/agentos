from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.db.enums import FindingCategory, FindingStatus, Severity

ScannerName = Literal["semgrep", "gitleaks", "slither", "forge", "echidna", "pip-audit"]


class ScopeCreate(BaseModel):
    project_id: UUID
    name: str = Field(min_length=1, max_length=200)
    local_path: str = Field(default=".", min_length=1, max_length=1000)
    excluded_paths: list[str] = Field(default_factory=list, max_length=100)
    external_targets: list[str] = Field(default_factory=list, max_length=100)
    authorization: str = Field(min_length=10, max_length=20000)
    allowed_tools: list[ScannerName] = Field(default_factory=list, max_length=6)
    expires_at: datetime | None = None


class ScopePublic(ScopeCreate):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    verified_by: UUID | None
    verified_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime


class FindingCreate(BaseModel):
    scope_id: UUID
    title: str = Field(min_length=1, max_length=400)
    category: FindingCategory = FindingCategory.WEB
    hypothesis: str = Field(min_length=10, max_length=20000)
    description: str = Field(default="", max_length=20000)
    severity: Severity = Severity.INFO
    confidence: float = Field(default=0.5, ge=0, le=1)
    cvss_score: Decimal | None = Field(default=None, ge=0, le=10)
    cwe_id: str | None = Field(default=None, pattern=r"^CWE-[1-9][0-9]*$")
    remediation: str = Field(default="", max_length=20000)


class FindingPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    scope_id: UUID | None
    project_id: UUID | None
    created_by: UUID | None
    title: str
    category: FindingCategory
    hypothesis: str
    description: str
    severity: Severity
    confidence: float
    cvss_score: Decimal | None
    cwe_id: str | None
    remediation: str
    status: FindingStatus
    meta: dict[str, Any]
    created_at: datetime


class EvidenceCreate(BaseModel):
    kind: Literal["note", "source", "validation", "scanner"] = "note"
    title: str = Field(min_length=1, max_length=300)
    content: str = Field(min_length=1, max_length=100000)
    description: str = Field(default="", max_length=20000)


class EvidencePublic(EvidenceCreate):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    finding_id: UUID
    content_hash: str
    created_at: datetime


class FindingTransition(BaseModel):
    status: FindingStatus
    rationale: str = Field(min_length=10, max_length=20000)


class ReviewCreate(BaseModel):
    approved: bool
    rationale: str = Field(min_length=10, max_length=20000)


class ReviewPublic(ReviewCreate):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    finding_id: UUID
    reviewer_id: UUID | None
    evidence_hash: str
    created_at: datetime


class ScanCreate(BaseModel):
    scope_id: UUID
    tool: ScannerName
    target_path: str = Field(default=".", min_length=1, max_length=1000)
    config_path: str | None = Field(default=None, max_length=1000)
    timeout_seconds: int = Field(default=120, ge=1, le=600)


class ScanPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    scope_id: UUID
    tool: str
    target_path: str
    status: str
    command: str
    exit_code: int | None
    output: dict[str, Any]
    error: str | None
    finished_at: datetime | None
    created_at: datetime
