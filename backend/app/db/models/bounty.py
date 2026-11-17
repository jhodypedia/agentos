from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import BaseModel
from app.db.enums import BountyPlatform, CompetitionStatus
from app.db.types import sa_enum


class BountyProgram(BaseModel):
    __tablename__ = "bounty_programs"
    __table_args__ = (Index("ix_bounty_programs_platform", "platform"),)

    slug: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    platform: Mapped[BountyPlatform] = mapped_column(
        sa_enum(BountyPlatform),
        default=BountyPlatform.MAKE_FUN,
        server_default=BountyPlatform.MAKE_FUN.value,
    )
    url: Mapped[str] = mapped_column(String(1000))
    description: Mapped[str] = mapped_column(Text, default="")
    scope: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="active", server_default="active")
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    competitions: Mapped[list[Competition]] = relationship(
        back_populates="program", cascade="all, delete-orphan", passive_deletes=True
    )


class Competition(BaseModel):
    __tablename__ = "competitions"
    __table_args__ = (
        UniqueConstraint("program_id", "external_id", name="uq_competitions_program_external"),
        Index("ix_competitions_status_deadline", "status", "deadline_at"),
        Index("ix_competitions_project", "project_id"),
    )

    program_id: Mapped[UUID] = mapped_column(
        ForeignKey("bounty_programs.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[UUID | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"))
    external_id: Mapped[str] = mapped_column(String(160))
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str] = mapped_column(String(1000), default="")
    requirements: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    scoring: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    reward_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    reward_currency: Mapped[str] = mapped_column(String(8), default="USD", server_default="USD")
    status: Mapped[CompetitionStatus] = mapped_column(
        sa_enum(CompetitionStatus),
        default=CompetitionStatus.DISCOVERED,
        server_default=CompetitionStatus.DISCOVERED.value,
    )
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    program: Mapped[BountyProgram] = relationship(back_populates="competitions")
