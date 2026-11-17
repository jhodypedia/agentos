from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import BaseModel
from app.db.enums import ModelUsageStatus, ProviderType
from app.db.types import sa_enum


class ModelProvider(BaseModel):
    __tablename__ = "model_providers"
    __table_args__ = (Index("ix_model_providers_enabled", "enabled"),)

    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(160))
    provider_type: Mapped[ProviderType] = mapped_column(
        sa_enum(ProviderType),
        default=ProviderType.OPENAI_COMPATIBLE,
        server_default=ProviderType.OPENAI_COMPATIBLE.value,
    )
    base_url: Mapped[str | None] = mapped_column(String(500))
    api_key_encrypted: Mapped[str | None] = mapped_column(Text)
    api_key_hint: Mapped[str | None] = mapped_column(String(8))
    default_model: Mapped[str | None] = mapped_column(String(160))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    last_health_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    health_status: Mapped[str | None] = mapped_column(String(32))


class ModelUsage(BaseModel):
    __tablename__ = "model_usage"
    __table_args__ = (
        Index("ix_model_usage_created", "created_at"),
        Index("ix_model_usage_agent_created", "agent_id", "created_at"),
        Index("ix_model_usage_task", "task_id"),
    )

    provider_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("model_providers.id", ondelete="SET NULL")
    )
    agent_id: Mapped[UUID | None] = mapped_column(ForeignKey("agents.id", ondelete="SET NULL"))
    task_id: Mapped[UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    model_name: Mapped[str] = mapped_column(String(160))
    request_id: Mapped[str | None] = mapped_column(String(120))
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), default=Decimal("0"), server_default=text("0")
    )
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[ModelUsageStatus] = mapped_column(
        sa_enum(ModelUsageStatus),
        default=ModelUsageStatus.SUCCESS,
        server_default=ModelUsageStatus.SUCCESS.value,
    )
    error: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
