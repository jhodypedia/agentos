from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import BaseModel
from app.db.enums import ExecutionEnvironment, RiskLevel
from app.db.types import sa_enum


class ToolRegistry(BaseModel):
    __tablename__ = "tool_registry"
    __table_args__ = (Index("ix_tool_registry_enabled_risk", "enabled", "risk_level"),)

    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    input_schema: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    output_schema: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    required_permissions: Mapped[list[str]] = mapped_column(JSONB, default=list)
    risk_level: Mapped[RiskLevel] = mapped_column(
        sa_enum(RiskLevel), default=RiskLevel.LOW, server_default=RiskLevel.LOW.value
    )
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=60, server_default=text("60"))
    execution_environment: Mapped[ExecutionEnvironment] = mapped_column(
        sa_enum(ExecutionEnvironment),
        default=ExecutionEnvironment.SANDBOX,
        server_default=ExecutionEnvironment.SANDBOX.value,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    version: Mapped[str] = mapped_column(String(32), default="1.0.0")
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
