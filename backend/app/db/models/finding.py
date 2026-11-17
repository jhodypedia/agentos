from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Float, ForeignKey, Index, Numeric, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import BaseModel
from app.db.enums import FindingCategory, FindingStatus, Severity
from app.db.types import sa_enum


class Finding(BaseModel):
    __tablename__ = "findings"
    __table_args__ = (
        Index("ix_findings_status_severity", "status", "severity"),
        Index("ix_findings_project_created", "project_id", "created_at"),
        Index("ix_findings_competition", "competition_id"),
    )

    project_id: Mapped[UUID | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"))
    scope_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("research_scopes.id", ondelete="SET NULL"), index=True
    )
    competition_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("competitions.id", ondelete="SET NULL")
    )
    task_id: Mapped[UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    agent_id: Mapped[UUID | None] = mapped_column(ForeignKey("agents.id", ondelete="SET NULL"))
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    title: Mapped[str] = mapped_column(String(400))
    category: Mapped[FindingCategory] = mapped_column(
        sa_enum(FindingCategory),
        default=FindingCategory.WEB,
        server_default=FindingCategory.WEB.value,
    )
    description: Mapped[str] = mapped_column(Text, default="")
    hypothesis: Mapped[str] = mapped_column(Text, default="")
    severity: Mapped[Severity] = mapped_column(
        sa_enum(Severity), default=Severity.INFO, server_default=Severity.INFO.value
    )
    status: Mapped[FindingStatus] = mapped_column(
        sa_enum(FindingStatus),
        default=FindingStatus.HYPOTHESIS,
        server_default=FindingStatus.HYPOTHESIS.value,
    )
    confidence: Mapped[float] = mapped_column(Float, default=0.5, server_default=text("0.5"))
    cvss_score: Mapped[Decimal | None] = mapped_column(Numeric(3, 1))
    cwe_id: Mapped[str | None] = mapped_column(String(32))
    remediation: Mapped[str] = mapped_column(Text, default="")
    evidence_summary: Mapped[str] = mapped_column(Text, default="")
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    evidence: Mapped[list[Evidence]] = relationship(
        back_populates="finding", cascade="all, delete-orphan", passive_deletes=True
    )


class Evidence(BaseModel):
    __tablename__ = "evidence"
    __table_args__ = (Index("ix_evidence_finding_created", "finding_id", "created_at"),)

    finding_id: Mapped[UUID] = mapped_column(
        ForeignKey("findings.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(40), default="note")
    title: Mapped[str] = mapped_column(String(300), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    content: Mapped[str] = mapped_column(Text, default="")
    content_hash: Mapped[str] = mapped_column(String(64))
    storage_path: Mapped[str | None] = mapped_column(String(1000))
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    finding: Mapped[Finding] = relationship(back_populates="evidence")
