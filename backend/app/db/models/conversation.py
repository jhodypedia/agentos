from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import BaseModel
from app.db.enums import ConversationChannel, ConversationStatus, MessageRole
from app.db.types import sa_enum


class Conversation(BaseModel):
    __tablename__ = "conversations"
    __table_args__ = (
        Index("ix_conversations_agent_created", "agent_id", "created_at"),
        Index("ix_conversations_user_created", "user_id", "created_at"),
        Index("ix_conversations_channel_status", "channel", "status"),
    )

    agent_id: Mapped[UUID | None] = mapped_column(ForeignKey("agents.id", ondelete="SET NULL"))
    task_id: Mapped[UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    title: Mapped[str] = mapped_column(String(300), default="")
    channel: Mapped[ConversationChannel] = mapped_column(
        sa_enum(ConversationChannel),
        default=ConversationChannel.WEB,
        server_default=ConversationChannel.WEB.value,
    )
    status: Mapped[ConversationStatus] = mapped_column(
        sa_enum(ConversationStatus),
        default=ConversationStatus.ACTIVE,
        server_default=ConversationStatus.ACTIVE.value,
    )
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    messages: Mapped[list[ConversationMessage]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class ConversationMessage(BaseModel):
    __tablename__ = "conversation_messages"
    __table_args__ = (
        Index("ix_conversation_messages_conversation_created", "conversation_id", "created_at"),
    )

    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[MessageRole] = mapped_column(
        sa_enum(MessageRole), default=MessageRole.USER, server_default=MessageRole.USER.value
    )
    content: Mapped[str] = mapped_column(Text, default="")
    tool_calls: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    tool_call_id: Mapped[str | None] = mapped_column(String(120))
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")
