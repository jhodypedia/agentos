from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.db.enums import UserRole


class UserPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: EmailStr
    display_name: str
    role: UserRole
    is_active: bool
    telegram_user_id: int | None
    last_login_at: datetime | None
    created_at: datetime


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class ApiKeyCreateRequest(BaseModel):
    name: str = Field(default="default", min_length=1, max_length=120)
    scopes: list[str] = Field(default_factory=list)
    expires_in_days: int | None = Field(default=None, ge=1, le=365)


class ApiKeyCreatedResponse(BaseModel):
    id: UUID
    name: str
    prefix: str
    key: str
    scopes: list[str]
    expires_at: datetime | None
    created_at: datetime


class ApiKeyPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    prefix: str
    scopes: list[str]
    last_used_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime
