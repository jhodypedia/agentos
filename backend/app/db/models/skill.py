from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import BaseModel


class Skill(BaseModel):
    __tablename__ = "skills"
    __table_args__ = (
        UniqueConstraint("slug", "version", name="uq_skills_slug_version"),
        Index("ix_skills_enabled", "enabled"),
    )

    slug: Mapped[str] = mapped_column(String(120), index=True)
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[str] = mapped_column(String(32), default="1.0.0")
    source: Mapped[str] = mapped_column(String(40), default="local")
    entry_path: Mapped[str] = mapped_column(String(500))
    directory_path: Mapped[str] = mapped_column(String(500))
    frontmatter: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class SkillExecution(BaseModel):
    """One recorded use of a skill by an agent.

    Skills are instructions rather than code, so "execution" means the skill was
    loaded into an agent's context (``load``) or that a tool call inside it failed
    or succeeded (``tool``). Keeping both makes the history answer "did this
    skill help?" rather than just "was it read?".
    """

    __tablename__ = "skill_executions"
    __table_args__ = (
        Index("ix_skill_executions_skill_created", "skill_id", "created_at"),
        Index("ix_skill_executions_task", "task_id"),
    )

    skill_id: Mapped[UUID] = mapped_column(
        ForeignKey("skills.id", ondelete="CASCADE"), nullable=False
    )
    agent_id: Mapped[UUID | None] = mapped_column(ForeignKey("agents.id", ondelete="SET NULL"))
    task_id: Mapped[UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    event: Mapped[str] = mapped_column(String(40), default="load")
    ok: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    detail: Mapped[str] = mapped_column(Text, default="")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
