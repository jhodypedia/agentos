"""Vector-search tests: the pgvector path, exercised with a stub embedder.

The deployment's model provider exposes no embedding model, so the live system
falls back to lexical retrieval. These tests replace the embedder with a
deterministic local one to verify the SQL path itself — the ``vector`` column,
``cosine_distance`` ordering and the hybrid score — actually works.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from math import sqrt
from typing import Any

import pytest

from app.db.enums import MemoryLayer
from app.db.session import get_session_factory
from app.memory import embeddings as embeddings_module
from app.services import memory as memory_service

pytestmark = pytest.mark.integration

DIMENSIONS = 1536


def _vector_for(text: str) -> list[float]:
    """Deterministic bag-of-words embedding: same text -> same direction."""
    vector = [0.0] * DIMENSIONS
    for word in text.lower().split():
        digest = hashlib.sha256(word.encode()).digest()
        index = int.from_bytes(digest[:4], "big") % DIMENSIONS
        vector[index] += 1.0
    norm = sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


class StubEmbedder:
    model = "stub-embedder-v1"

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str], *, input_type: str = "document") -> list[list[float]]:
        self.calls.append(list(texts))
        return [_vector_for(text) for text in texts]


@pytest.fixture(autouse=True)
async def stub_embedder(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[StubEmbedder]:
    stub = StubEmbedder()

    async def fake_resolve(db: Any) -> StubEmbedder:
        return stub

    monkeypatch.setattr(embeddings_module, "resolve_embedder", fake_resolve)
    monkeypatch.setattr(memory_service, "resolve_embedder", fake_resolve)
    yield stub


async def test_write_stores_a_vector_of_the_configured_width() -> None:
    factory = get_session_factory()
    async with factory() as session:
        memory = await memory_service.write_memory(
            session,
            memory_service.MemoryWrite(
                content="pgvector stores embeddings in a fixed-width column.",
                layer=MemoryLayer.SEMANTIC,
                source="test",
            ),
        )
        await session.commit()
        await session.refresh(memory)

        assert memory.embedding_vec is not None
        assert len(memory.embedding_vec) == DIMENSIONS
        assert memory.embedding_dim == DIMENSIONS
        assert memory.embedding_model


async def test_hybrid_search_reports_a_vector_score() -> None:
    factory = get_session_factory()
    async with factory() as session:
        await memory_service.write_memory(
            session,
            memory_service.MemoryWrite(
                content="Argon2id hashes passwords with a memory cost.",
                layer=MemoryLayer.SEMANTIC,
                source="test",
            ),
        )
        await memory_service.write_memory(
            session,
            memory_service.MemoryWrite(
                content="ECharts renders the dashboard charts.",
                layer=MemoryLayer.SEMANTIC,
                source="test",
            ),
        )
        await session.commit()

        hits = await memory_service.retrieve(session, query="password hashing memory cost", limit=5)

    assert hits
    assert hits[0].vector is not None
    assert hits[0].vector > 0.0
    assert hits[0].lexical > 0.0
    assert hits[0].memory.content.startswith("Argon2id")
    assert hits[0].score > hits[-1].score


async def test_vector_search_orders_by_cosine_distance() -> None:
    factory = get_session_factory()
    async with factory() as session:
        await memory_service.write_memory(
            session,
            memory_service.MemoryWrite(
                content="Kubernetes ingress terminates TLS.",
                layer=MemoryLayer.SEMANTIC,
                source="test",
            ),
        )
        await memory_service.write_memory(
            session,
            memory_service.MemoryWrite(
                content="Celery workers process background jobs.",
                layer=MemoryLayer.SEMANTIC,
                source="test",
            ),
        )
        await session.commit()

        hits = await memory_service.vector_search(session, query="kubernetes ingress tls", limit=2)

    assert hits
    assert hits[0].memory.content.startswith("Kubernetes")
    assert hits[0].score > hits[1].score


async def test_agent_isolation_applies_to_vector_search() -> None:
    from app.db.enums import AgentStatus
    from app.db.models import Agent

    factory = get_session_factory()
    async with factory() as session:
        agent_a = Agent(slug="vec-agent-a", name="A", status=AgentStatus.ACTIVE)
        agent_b = Agent(slug="vec-agent-b", name="B", status=AgentStatus.ACTIVE)
        session.add_all([agent_a, agent_b])
        await session.flush()

        await memory_service.write_memory(
            session,
            memory_service.MemoryWrite(
                content="Agent A keeps secrets about vector indexes.",
                layer=MemoryLayer.AGENT,
                agent_id=agent_a.id,
                source="test",
            ),
        )
        await memory_service.write_memory(
            session,
            memory_service.MemoryWrite(
                content="Agent B keeps notes about vector indexes.",
                layer=MemoryLayer.AGENT,
                agent_id=agent_b.id,
                source="test",
            ),
        )
        await session.commit()

        hits = await memory_service.vector_search(
            session, query="vector indexes", agent_id=agent_b.id, limit=5
        )

    contents = [hit.memory.content for hit in hits]
    assert contents == ["Agent B keeps notes about vector indexes."]


async def test_embedding_failure_degrades_to_lexical(monkeypatch: pytest.MonkeyPatch) -> None:
    class BrokenEmbedder:
        async def embed(
            self, texts: list[str], *, input_type: str = "document"
        ) -> list[list[float]]:
            raise RuntimeError("upstream embedding service is down")

    async def broken_resolve(db: Any) -> BrokenEmbedder:
        return BrokenEmbedder()

    monkeypatch.setattr(memory_service, "resolve_embedder", broken_resolve)

    factory = get_session_factory()
    async with factory() as session:
        await memory_service.write_memory(
            session,
            memory_service.MemoryWrite(
                content="Fallback search still finds lexical matches.",
                layer=MemoryLayer.SEMANTIC,
                source="test",
                embed=False,
            ),
        )
        await session.commit()

        hits = await memory_service.retrieve(session, query="lexical matches", limit=5)

    assert hits
    assert hits[0].vector is None
    assert hits[0].lexical > 0
