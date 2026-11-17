from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Message(BaseModel):
    detail: str


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    request_id: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


class PageMeta(BaseModel):
    total: int
    limit: int
    offset: int


class Paginated(BaseModel):
    meta: PageMeta
    items: list[Any]


class IdResponse(BaseModel):
    id: UUID
