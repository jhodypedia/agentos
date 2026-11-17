"""Our entry into an external bounty competition.

A ``Competition`` is the platform's mirror of a listing somebody else owns; an
``CompetitionEntry`` is one attempt to win it. Keeping them apart matters: a
competition can receive several entries over time (the API allows retries until
the deadline), and each entry has its own repo commit, transcripts, approval and
outcome.

Nothing here is written until the platform reports it. ``checks`` holds the
verdicts the API returned, verbatim, so a rejection can be diagnosed without
guessing, and ``submission_id`` is the platform's own id, never a local
invention.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import BaseModel
from app.db.enums import CompetitionEntryStatus
from app.db.types import sa_enum


class CompetitionEntry(BaseModel):
    __tablename__ = "competition_entries"
    __table_args__ = (
        Index("ix_competition_entries_status_created", "status", "created_at"),
        Index("ix_competition_entries_competition", "competition_id"),
        Index("ix_competition_entries_submission", "submission_id"),
    )

    competition_id: Mapped[UUID] = mapped_column(
        ForeignKey("competitions.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[UUID | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"))
    approval_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("approvals.id", ondelete="SET NULL")
    )
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    status: Mapped[CompetitionEntryStatus] = mapped_column(
        sa_enum(CompetitionEntryStatus),
        default=CompetitionEntryStatus.DRAFT,
        server_default=CompetitionEntryStatus.DRAFT.value,
    )
    # The platform's identifier for this entry; null until it accepts one.
    submission_id: Mapped[str | None] = mapped_column(String(120))
    title: Mapped[str] = mapped_column(String(300), default="")
    repo_url: Mapped[str] = mapped_column(String(1000), default="")
    commit_sha: Mapped[str] = mapped_column(String(64), default="")

    # Paths inside the project workspace, preserved so the transcripts that were
    # actually uploaded can be re-read later. Platform rules require authentic
    # transcripts, so they are never regenerated.
    transcript_paths: Mapped[list[str]] = mapped_column(JSONB, default=list)
    checks: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    score: Mapped[str | None] = mapped_column(String(64))
    rank: Mapped[int | None] = mapped_column(Integer)
    prize_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    error: Mapped[str | None] = mapped_column(Text)

    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    competition: Mapped[Any] = relationship("Competition")
