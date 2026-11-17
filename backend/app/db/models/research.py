"""Authorization, scanner runs and independent finding reviews."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import BaseModel


class ResearchScope(BaseModel):
    __tablename__ = "research_scopes"
    __table_args__ = (Index("ix_research_scopes_project", "project_id"),)

    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200))
    local_path: Mapped[str] = mapped_column(String(1000), default=".")
    excluded_paths: Mapped[list[str]] = mapped_column(JSONB, default=list)
    external_targets: Mapped[list[str]] = mapped_column(JSONB, default=list)
    authorization: Mapped[str] = mapped_column(Text)
    allowed_tools: Mapped[list[str]] = mapped_column(JSONB, default=list)
    verified_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SecurityScan(BaseModel):
    __tablename__ = "security_scans"
    __table_args__ = (Index("ix_security_scans_scope_created", "scope_id", "created_at"),)

    scope_id: Mapped[UUID] = mapped_column(ForeignKey("research_scopes.id", ondelete="CASCADE"))
    requested_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    tool: Mapped[str] = mapped_column(String(40))
    target_path: Mapped[str] = mapped_column(String(1000))
    status: Mapped[str] = mapped_column(String(32), default="running")
    command: Mapped[str] = mapped_column(Text, default="")
    exit_code: Mapped[int | None] = mapped_column(Integer)
    output: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FindingReview(BaseModel):
    __tablename__ = "finding_reviews"
    __table_args__ = (Index("ix_finding_reviews_finding_created", "finding_id", "created_at"),)

    finding_id: Mapped[UUID] = mapped_column(ForeignKey("findings.id", ondelete="CASCADE"))
    reviewer_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    approved: Mapped[bool] = mapped_column(Boolean)
    rationale: Mapped[str] = mapped_column(Text)
    evidence_hash: Mapped[str] = mapped_column(String(64))
