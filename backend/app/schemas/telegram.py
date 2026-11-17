from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class TelegramLink(BaseModel):
    user_id: UUID
    telegram_user_id: int | None = Field(default=None, gt=0)


class WebhookSetup(BaseModel):
    url: str = Field(min_length=10, max_length=1000)


class TelegramUpdatePayload(BaseModel):
    update_id: int = Field(ge=0)
    message: dict[str, Any] | None = None
    callback_query: dict[str, Any] | None = None
