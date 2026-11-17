from __future__ import annotations

from typing import Any

from sqlalchemy import BigInteger, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import BaseModel


class TelegramUpdate(BaseModel):
    __tablename__ = "telegram_updates"

    update_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(32), default="received")
    response: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
