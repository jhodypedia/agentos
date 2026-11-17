"""Optional text embeddings for semantic memory search.

Embeddings are a capability, not a requirement: with no embedding provider
configured, memory retrieval falls back to lexical scoring and every vector
column stays null. Nothing here fabricates a vector — if the provider is down,
the caller gets :class:`EmbeddingError` and the memory is stored without one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import UnprocessableError
from app.core.security import decrypt_secret
from app.db.models import ModelProvider

logger = structlog.get_logger("agentos.memory.embeddings")

_MAX_INPUT_CHARS = 8_000


class EmbeddingError(Exception):
    """The embedding provider could not produce vectors."""


@dataclass(slots=True)
class Embedder:
    """A configured embeddings endpoint."""

    model: str
    base_url: str
    api_key: str | None
    dimensions: int
    timeout_seconds: float = 20.0

    async def embed(self, texts: list[str], *, input_type: str = "document") -> list[list[float]]:
        if not texts:
            return []
        payload: dict[str, Any] = {
            "model": self.model,
            "input": [text[:_MAX_INPUT_CHARS] for text in texts],
        }
        if self.dimensions and self.model.startswith("text-embedding-3"):
            payload["dimensions"] = self.dimensions
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(
                    f"{self.base_url.rstrip('/')}/embeddings", json=payload, headers=headers
                )
        except httpx.HTTPError as exc:
            raise EmbeddingError(f"embedding request failed: {exc}") from exc

        if response.status_code >= 400:
            raise EmbeddingError(
                f"embedding request was rejected (HTTP {response.status_code}): "
                f"{response.text[:200]}"
            )

        try:
            body = response.json()
            rows = body["data"]
            vectors = [row["embedding"] for row in rows]
        except (ValueError, KeyError, TypeError) as exc:
            raise EmbeddingError("embedding response was malformed") from exc

        if len(vectors) != len(texts):
            raise EmbeddingError(
                f"embedding response had {len(vectors)} vectors for {len(texts)} inputs"
            )
        for vector in vectors:
            if not isinstance(vector, list) or len(vector) != self.dimensions:
                raise EmbeddingError(
                    f"embedding has {len(vector) if isinstance(vector, list) else 0} "
                    f"dimensions, expected {self.dimensions}"
                )
        return [[float(value) for value in vector] for vector in vectors]


async def resolve_embedder(db: AsyncSession) -> Embedder | None:
    """Build an embedder from the configured provider, or ``None`` if unset."""
    settings = get_settings()
    slug = settings.memory_embedding_provider
    model = settings.memory_embedding_model
    if not slug or not model:
        return None

    if settings.memory_embedding_dimensions != PGVECTOR_DIMENSIONS:
        logger.warning(
            "embedding_dimensions_unsupported",
            configured=settings.memory_embedding_dimensions,
            supported=PGVECTOR_DIMENSIONS,
            detail="memory falls back to lexical retrieval",
        )
        return None

    record = (
        await db.execute(select(ModelProvider).where(ModelProvider.slug == slug))
    ).scalar_one_or_none()
    if record is None:
        logger.warning("embedding_provider_missing", slug=slug)
        return None
    if not record.enabled or not record.base_url:
        logger.warning("embedding_provider_unusable", slug=slug)
        return None

    api_key: str | None = None
    if record.api_key_encrypted:
        try:
            api_key = decrypt_secret(record.api_key_encrypted)
        except ValueError:
            logger.warning("embedding_provider_key_unreadable", slug=slug)
            return None

    return Embedder(
        model=model,
        base_url=record.base_url,
        api_key=api_key,
        dimensions=settings.memory_embedding_dimensions,
        timeout_seconds=settings.memory_embedding_timeout_seconds,
    )


def cosine_similarity(left: list[float], right: list[float]) -> float:
    """Cosine similarity in ``[-1, 1]``; ``0.0`` for incomparable vectors."""
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = 0.0
    left_norm = 0.0
    right_norm = 0.0
    for a, b in zip(left, right, strict=True):
        dot += a * b
        left_norm += a * a
        right_norm += b * b
    if left_norm <= 0.0 or right_norm <= 0.0:
        return 0.0
    return float(dot / ((left_norm**0.5) * (right_norm**0.5)))


# The ``memories.embedding_vec`` column is fixed-width, so an embedder with a
# different dimensionality would make every insert fail. Refusing it up front
# degrades memory to lexical search instead of breaking writes.
PGVECTOR_DIMENSIONS = 1536


def ensure_embedding_configured() -> None:
    settings = get_settings()
    if not settings.memory_embedding_provider or not settings.memory_embedding_model:
        raise UnprocessableError(
            "Semantic search needs MEMORY_EMBEDDING_PROVIDER and MEMORY_EMBEDDING_MODEL"
        )
