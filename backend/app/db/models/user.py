from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import BaseModel
from app.db.enums import UserRole
from app.db.types import sa_enum


class User(BaseModel):
    __tablename__ = "users"
    __table_args__ = (Index("ix_users_role_active", "role", "is_active"),)

    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(120), default="Operator")
    hashed_password: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[UserRole] = mapped_column(
        sa_enum(UserRole), default=UserRole.VIEWER, server_default=UserRole.VIEWER.value
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    api_keys: Mapped[list[ApiKey]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )


class ApiKey(BaseModel):
    __tablename__ = "api_keys"
    __table_args__ = (Index("ix_api_keys_user_revoked", "user_id", "revoked_at"),)

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), default="default")
    prefix: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    scopes: Mapped[list[str]] = mapped_column(JSONB, default=list)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="api_keys")
