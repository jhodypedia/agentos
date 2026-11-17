from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import BaseModel


class AuditLog(BaseModel):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_created", "created_at"),
        Index("ix_audit_logs_resource", "resource_type", "resource_id"),
        Index("ix_audit_logs_actor_created", "actor_user_id", "created_at"),
    )

    actor_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    actor_agent_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("agents.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(String(120))
    resource_type: Mapped[str] = mapped_column(String(80))
    resource_id: Mapped[str | None] = mapped_column(String(80))
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(400))
    success: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
