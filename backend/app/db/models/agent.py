from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import BaseModel
from app.db.enums import AgentStatus
from app.db.types import sa_enum


class Agent(BaseModel):
    __tablename__ = "agents"
    __table_args__ = (Index("ix_agents_status", "status"),)

    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    system_prompt: Mapped[str] = mapped_column(Text, default="")

    model_provider_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("model_providers.id", ondelete="SET NULL")
    )
    model_name: Mapped[str | None] = mapped_column(String(160))
    temperature: Mapped[float] = mapped_column(Float, default=0.2, server_default=text("0.2"))
    top_p: Mapped[float] = mapped_column(Float, default=1.0, server_default=text("1.0"))
    max_output_tokens: Mapped[int] = mapped_column(
        Integer, default=4096, server_default=text("4096")
    )

    max_iterations: Mapped[int] = mapped_column(Integer, default=25, server_default=text("25"))
    token_budget: Mapped[int] = mapped_column(
        Integer, default=200_000, server_default=text("200000")
    )
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=1800, server_default=text("1800"))

    memory_config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    permissions: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    status: Mapped[AgentStatus] = mapped_column(
        sa_enum(AgentStatus), default=AgentStatus.DRAFT, server_default=AgentStatus.DRAFT.value
    )
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))

    parent_agent_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("agents.id", ondelete="SET NULL")
    )
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    tools: Mapped[list[AgentTool]] = relationship(
        back_populates="agent", cascade="all, delete-orphan", passive_deletes=True
    )
    skills: Mapped[list[AgentSkill]] = relationship(
        back_populates="agent", cascade="all, delete-orphan", passive_deletes=True
    )
    configs: Mapped[list[AgentConfig]] = relationship(
        back_populates="agent", cascade="all, delete-orphan", passive_deletes=True
    )


class AgentConfig(BaseModel):
    __tablename__ = "agent_configs"
    __table_args__ = (Index("ix_agent_configs_agent_key", "agent_id", "key", unique=True),)

    agent_id: Mapped[UUID] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(120))
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    description: Mapped[str] = mapped_column(Text, default="")

    agent: Mapped[Agent] = relationship(back_populates="configs")


class AgentTool(BaseModel):
    __tablename__ = "agent_tools"
    __table_args__ = (Index("ix_agent_tools_agent_enabled", "agent_id", "enabled"),)

    agent_id: Mapped[UUID] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    tool_id: Mapped[UUID] = mapped_column(
        ForeignKey("tool_registry.id", ondelete="CASCADE"), nullable=False
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    permission_overrides: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    agent: Mapped[Agent] = relationship(back_populates="tools")


class AgentSkill(BaseModel):
    __tablename__ = "agent_skills"
    __table_args__ = (Index("ix_agent_skills_agent_enabled", "agent_id", "enabled"),)

    agent_id: Mapped[UUID] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    skill_id: Mapped[UUID] = mapped_column(
        ForeignKey("skills.id", ondelete="CASCADE"), nullable=False
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    agent: Mapped[Agent] = relationship(back_populates="skills")
