from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import BaseModel
from app.db.enums import ApprovalAction, ApprovalStatus
from app.db.types import sa_enum


class Approval(BaseModel):
    __tablename__ = "approvals"
    __table_args__ = (
        Index("ix_approvals_status_created", "status", "created_at"),
        Index("ix_approvals_task", "task_id"),
        Index("ix_approvals_assigned_status", "assigned_to_user_id", "status"),
    )

    task_id: Mapped[UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    step_id: Mapped[UUID | None] = mapped_column(ForeignKey("task_steps.id", ondelete="SET NULL"))
    finding_id: Mapped[UUID | None] = mapped_column(ForeignKey("findings.id", ondelete="SET NULL"))
    requested_by_agent_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("agents.id", ondelete="SET NULL")
    )
    assigned_to_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    decided_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

    action_type: Mapped[ApprovalAction] = mapped_column(
        sa_enum(ApprovalAction),
        default=ApprovalAction.OTHER,
        server_default=ApprovalAction.OTHER.value,
    )
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    status: Mapped[ApprovalStatus] = mapped_column(
        sa_enum(ApprovalStatus),
        default=ApprovalStatus.PENDING,
        server_default=ApprovalStatus.PENDING.value,
    )
    decision_note: Mapped[str] = mapped_column(Text, default="")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
