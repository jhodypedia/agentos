from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import BaseModel
from app.db.enums import CodingSessionStatus
from app.db.types import sa_enum


class CodingSession(BaseModel):
    """One invocation of a coding engine inside a project workspace.

    Competition rules sometimes require the original transcript, so the raw
    engine output is preserved at ``transcript_path`` and never regenerated.
    The row is the audit trail: engine, instruction, outcome and diff summary.
    """

    __tablename__ = "coding_sessions"
    __table_args__ = (
        Index("ix_coding_sessions_project_created", "project_id", "created_at"),
        Index("ix_coding_sessions_task", "task_id"),
    )

    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    task_id: Mapped[UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    agent_id: Mapped[UUID | None] = mapped_column(ForeignKey("agents.id", ondelete="SET NULL"))
    requested_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    engine: Mapped[str] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(200))
    session_id: Mapped[str | None] = mapped_column(String(200), index=True)
    status: Mapped[CodingSessionStatus] = mapped_column(
        sa_enum(CodingSessionStatus),
        default=CodingSessionStatus.RUNNING,
        server_default=CodingSessionStatus.RUNNING.value,
    )
    instruction: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text, default="")
    transcript_path: Mapped[str | None] = mapped_column(String(1000))
    exit_code: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    changes: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    commands: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    error: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    project: Mapped[Any] = relationship("Project")
