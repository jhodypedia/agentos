from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import BaseModel
from app.db.enums import MemoryLayer
from app.db.types import sa_enum


class Memory(BaseModel):
    """A single memory record.

    ``embedding`` keeps the provider-neutral float array for auditing, while
    ``embedding_vec`` is the pgvector column used for similarity search. Both are
    written together; ``embedding_dim`` records which model produced them so a
    model change never silently compares incompatible vectors.
    """

    __tablename__ = "memories"
    __table_args__ = (
        Index("ix_memories_agent_layer", "agent_id", "layer"),
        Index("ix_memories_project_layer", "project_id", "layer"),
        Index("ix_memories_layer_created", "layer", "created_at"),
        Index("ix_memories_content_hash", "content_hash"),
        Index("ix_memories_expires", "expires_at"),
        Index(
            "ix_memories_embedding",
            "embedding_vec",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding_vec": "vector_cosine_ops"},
        ),
    )

    agent_id: Mapped[UUID | None] = mapped_column(ForeignKey("agents.id", ondelete="SET NULL"))
    project_id: Mapped[UUID | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"))
    task_id: Mapped[UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    parent_memory_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("memories.id", ondelete="SET NULL")
    )

    layer: Mapped[MemoryLayer] = mapped_column(
        sa_enum(MemoryLayer),
        default=MemoryLayer.SEMANTIC,
        server_default=MemoryLayer.SEMANTIC.value,
    )
    key: Mapped[str | None] = mapped_column(String(200))
    content: Mapped[str] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    embedding: Mapped[list[float] | None] = mapped_column(JSONB)
    embedding_vec: Mapped[list[float] | None] = mapped_column(Vector(1536))
    embedding_model: Mapped[str | None] = mapped_column(String(160))
    embedding_dim: Mapped[int | None] = mapped_column(Integer)

    source: Mapped[str] = mapped_column(String(200), default="agent")
    source_ref: Mapped[str | None] = mapped_column(String(500))
    content_hash: Mapped[str] = mapped_column(String(64))
    dedup_key: Mapped[str | None] = mapped_column(String(300), index=True)

    importance: Mapped[float] = mapped_column(Float, default=0.5, server_default=text("0.5"))
    confidence: Mapped[float] = mapped_column(Float, default=0.5, server_default=text("0.5"))
    version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    is_sensitive: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    access_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
