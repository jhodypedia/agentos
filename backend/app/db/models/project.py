from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import BaseModel
from app.db.enums import ProjectStatus, RepositoryProvider
from app.db.types import sa_enum


class Project(BaseModel):
    __tablename__ = "projects"
    __table_args__ = (Index("ix_projects_status_created", "status", "created_at"),)

    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    owner_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    workspace_path: Mapped[str] = mapped_column(String(500), unique=True)
    status: Mapped[ProjectStatus] = mapped_column(
        sa_enum(ProjectStatus),
        default=ProjectStatus.ACTIVE,
        server_default=ProjectStatus.ACTIVE.value,
    )
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    repositories: Mapped[list[Repository]] = relationship(
        back_populates="project", cascade="all, delete-orphan", passive_deletes=True
    )


class Repository(BaseModel):
    __tablename__ = "repositories"
    __table_args__ = (
        UniqueConstraint("project_id", "url", name="uq_repositories_project_url"),
        Index("ix_repositories_project", "project_id"),
    )

    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(String(1000))
    provider: Mapped[RepositoryProvider] = mapped_column(
        sa_enum(RepositoryProvider),
        default=RepositoryProvider.GITHUB,
        server_default=RepositoryProvider.GITHUB.value,
    )
    default_branch: Mapped[str] = mapped_column(String(200), default="main")
    local_path: Mapped[str | None] = mapped_column(String(500))
    last_analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    project: Mapped[Project] = relationship(back_populates="repositories")
