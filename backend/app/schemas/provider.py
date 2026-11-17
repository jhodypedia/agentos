"""Model provider request/response schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.db.enums import ProviderType


class ProviderCreate(BaseModel):
    slug: str = Field(min_length=2, max_length=80, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    name: str = Field(min_length=1, max_length=160)
    provider_type: ProviderType
    base_url: str | None = Field(default=None, max_length=500)
    api_key: str | None = Field(default=None, min_length=1, max_length=1000)
    default_model: str | None = Field(default=None, max_length=160)
    enabled: bool = False
    config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("base_url")
    @classmethod
    def _validate_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            return None
        if not stripped.startswith(("http://", "https://")):
            raise ValueError("base_url must start with http:// or https://")
        return stripped


class ProviderUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    base_url: str | None = Field(default=None, max_length=500)
    api_key: str | None = Field(default=None, min_length=1, max_length=1000)
    clear_api_key: bool = False
    default_model: str | None = Field(default=None, max_length=160)
    enabled: bool | None = None
    config: dict[str, Any] | None = None


class ProviderPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    slug: str
    name: str
    provider_type: ProviderType
    base_url: str | None
    api_key_hint: str | None
    default_model: str | None
    enabled: bool
    health_status: str | None
    last_health_check_at: datetime | None
    config: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class ProviderHealthResponse(BaseModel):
    ok: bool
    detail: str = ""
    models: list[str] = Field(default_factory=list)
    latency_ms: int | None = None


class ProviderUsageResponse(BaseModel):
    requests: int
    total_tokens: int
    cost_usd: str
