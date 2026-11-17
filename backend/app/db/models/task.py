from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import BaseModel
from app.db.enums import (
    EventLevel,
    StepKind,
    StepStatus,
    TaskDependencyType,
    TaskStatus,
)
from app.db.types import sa_enum


class Task(BaseModel):
    __tablename__ = "tasks"
    __table_args__ = (
        Index("ix_tasks_status_created", "status", "created_at"),
        Index("ix_tasks_assigned_status", "assigned_agent_id", "status"),
        Index("ix_tasks_project_created", "project_id", "created_at"),
        Index("ix_tasks_deadline", "deadline_at"),
        Index("ix_tasks_claimable", "status", "scheduled_at", "lease_expires_at"),
    )

    project_id: Mapped[UUID | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"))
    parent_task_id: Mapped[UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    assigned_agent_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("agents.id", ondelete="SET NULL")
    )

    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text, default="")
    objective: Mapped[str] = mapped_column(Text)
    status: Mapped[TaskStatus] = mapped_column(
        sa_enum(TaskStatus), default=TaskStatus.PENDING, server_default=TaskStatus.PENDING.value
    )
    priority: Mapped[int] = mapped_column(Integer, default=100, server_default=text("100"))

    input: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    output: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    error: Mapped[str | None] = mapped_column(Text)

    budget_tokens: Mapped[int | None] = mapped_column(Integer)
    used_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    budget_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), default=Decimal("0"), server_default=text("0")
    )

    max_iterations: Mapped[int] = mapped_column(Integer, default=50, server_default=text("50"))
    used_iterations: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=3600, server_default=text("3600"))

    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))

    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # A worker claims a task by taking a lease. The lease is what makes a crash
    # recoverable: a task whose owner stopped heartbeating becomes eligible again
    # instead of staying RUNNING forever. Nothing else may claim a live lease.
    claimed_by: Mapped[str | None] = mapped_column(String(200))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    correlation_id: Mapped[UUID | None] = mapped_column(index=True)
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    steps: Mapped[list[TaskStep]] = relationship(
        back_populates="task", cascade="all, delete-orphan", passive_deletes=True
    )
    events: Mapped[list[TaskEvent]] = relationship(
        back_populates="task", cascade="all, delete-orphan", passive_deletes=True
    )


class TaskStep(BaseModel):
    __tablename__ = "task_steps"
    __table_args__ = (
        UniqueConstraint("task_id", "seq", name="uq_task_steps_task_seq"),
        Index("ix_task_steps_task_status", "task_id", "status"),
        Index("ix_task_steps_agent_status", "agent_id", "status"),
    )

    task_id: Mapped[UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    parent_step_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("task_steps.id", ondelete="SET NULL")
    )
    agent_id: Mapped[UUID | None] = mapped_column(ForeignKey("agents.id", ondelete="SET NULL"))

    seq: Mapped[int] = mapped_column(Integer)
    kind: Mapped[StepKind] = mapped_column(
        sa_enum(StepKind), default=StepKind.LLM, server_default=StepKind.LLM.value
    )
    status: Mapped[StepStatus] = mapped_column(
        sa_enum(StepStatus), default=StepStatus.PENDING, server_default=StepStatus.PENDING.value
    )
    title: Mapped[str] = mapped_column(String(300), default="")
    description: Mapped[str] = mapped_column(Text, default="")

    input: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    output: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    attempt: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, server_default=text("3"))
    depends_on: Mapped[list[str]] = mapped_column(JSONB, default=list)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    task: Mapped[Task] = relationship(back_populates="steps")


class TaskDependency(BaseModel):
    __tablename__ = "task_dependencies"
    __table_args__ = (
        UniqueConstraint("task_id", "depends_on_task_id", name="uq_task_dependencies_pair"),
        CheckConstraint("task_id <> depends_on_task_id", name="task_dependency_no_self_reference"),
        Index("ix_task_dependencies_depends_on", "depends_on_task_id"),
    )

    task_id: Mapped[UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    depends_on_task_id: Mapped[UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    dependency_type: Mapped[TaskDependencyType] = mapped_column(
        sa_enum(TaskDependencyType),
        default=TaskDependencyType.FINISH_TO_START,
        server_default=TaskDependencyType.FINISH_TO_START.value,
    )


class TaskEvent(BaseModel):
    __tablename__ = "task_events"
    __table_args__ = (
        Index("ix_task_events_task_created", "task_id", "created_at"),
        Index("ix_task_events_type_created", "event_type", "created_at"),
    )

    task_id: Mapped[UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    step_id: Mapped[UUID | None] = mapped_column(ForeignKey("task_steps.id", ondelete="SET NULL"))
    agent_id: Mapped[UUID | None] = mapped_column(ForeignKey("agents.id", ondelete="SET NULL"))
    event_type: Mapped[str] = mapped_column(String(80))
    level: Mapped[EventLevel] = mapped_column(
        sa_enum(EventLevel), default=EventLevel.INFO, server_default=EventLevel.INFO.value
    )
    message: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    task: Mapped[Task] = relationship(back_populates="events")


class AgentMessage(BaseModel):
    __tablename__ = "agent_messages"
    __table_args__ = (
        Index("ix_agent_messages_task_created", "task_id", "created_at"),
        Index("ix_agent_messages_to_created", "to_agent_id", "created_at"),
    )

    task_id: Mapped[UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    conversation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL")
    )
    from_agent_id: Mapped[UUID | None] = mapped_column(ForeignKey("agents.id", ondelete="SET NULL"))
    to_agent_id: Mapped[UUID | None] = mapped_column(ForeignKey("agents.id", ondelete="SET NULL"))
    message_type: Mapped[str] = mapped_column(String(40), default="message")
    content: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
