from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Boolean, ForeignKey, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import BaseModel


class SystemSetting(BaseModel):
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    description: Mapped[str] = mapped_column(Text, default="")
    is_secret: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    updated_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
