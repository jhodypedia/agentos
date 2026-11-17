"""Memory service: write, retrieve, version, deduplicate and summarise.

Layers are not separate tables — they are one table with a ``layer`` enum and a
scope (agent / project / task / user). The differences between layers are
behavioural:

* ``working`` memories are ephemeral task scratch space; they expire with the
  task and are never shared.
* ``conversation`` memories capture recent turns for a task.
* ``episodic`` records what happened on a finished task and its outcome.
* ``semantic`` holds reusable technical knowledge.
* ``project`` is repository-specific and only visible inside its project.
* ``agent`` is an agent's own experience and preferences.

Isolation is enforced at the query level: every scope recorded on a memory
(agent, project, task, user) is required again on read, so a project memory
cannot be read from a different project, an agent's private memory cannot be
read by another agent, and one user's memory is not listed to another.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Select, false, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import NotFoundError, UnprocessableError
from app.db.enums import MemoryLayer
from app.db.models import Memory, Task
from app.memory.embeddings import resolve_embedder
from app.memory.scoring import (
    DEFAULT_WEIGHTS,
    ScoreWeights,
    combine,
    lexical_score,
    recency_score,
    tokenize,
    usage_score,
)
from app.memory.sensitivity import inspect as inspect_sensitivity

logger = structlog.get_logger("agentos.memory")

_WHITESPACE = re.compile(r"\s+")
_SHARED_LAYERS = frozenset(
    {MemoryLayer.SEMANTIC, MemoryLayer.PROJECT, MemoryLayer.AGENT, MemoryLayer.EPISODIC}
)
_PROJECT_SCOPED = frozenset({MemoryLayer.PROJECT})


class MemorySensitivityError(UnprocessableError):
    code = "memory_sensitive_content"
    message = "Memory was rejected because it appears to contain a secret"


@dataclass(slots=True)
class MemoryWrite:
    """Everything needed to persist one memory."""

    content: str
    layer: MemoryLayer = MemoryLayer.SEMANTIC
    agent_id: UUID | None = None
    project_id: UUID | None = None
    task_id: UUID | None = None
    user_id: UUID | None = None
    key: str | None = None
    summary: str | None = None
    source: str = "agent"
    source_ref: str | None = None
    importance: float = 0.5
    confidence: float = 0.5
    dedup_key: str | None = None
    parent_memory_id: UUID | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    ttl_days: int | None = None
    embed: bool = True


@dataclass(slots=True)
class MemoryHit:
    memory: Memory
    score: float
    lexical: float
    recency: float
    usage: float
    vector: float | None

    def explain(self) -> dict[str, float | None]:
        return {
            "score": round(self.score, 4),
            "lexical": round(self.lexical, 4),
            "recency": round(self.recency, 4),
            "usage": round(self.usage, 4),
            "vector": None if self.vector is None else round(self.vector, 4),
        }


def normalise_content(content: str) -> str:
    return _WHITESPACE.sub(" ", content.strip())


def content_digest(content: str) -> str:
    return hashlib.sha256(normalise_content(content).lower().encode("utf-8")).hexdigest()


def default_dedup_key(write: MemoryWrite) -> str:
    """Scope + layer + key when a key is given, otherwise the content digest."""
    scope = ":".join(
        [
            write.layer.value,
            str(write.project_id or "-"),
            str(write.agent_id or "-"),
            write.key or "",
        ]
    )
    digest = content_digest(write.content) if not write.key else ""
    return f"{scope}:{digest}"[:300]


async def write_memory(db: AsyncSession, write: MemoryWrite) -> Memory:
    """Store a memory, deduplicating and versioning as required.

    Writing the same content in the same scope twice does not create a second
    row: it bumps ``access_count`` and refreshes ``accessed_at``. Writing
    *different* content under the same ``key`` creates a new version that points
    at its predecessor through ``parent_memory_id`` and leaves the old row in
    place, so history is never destroyed.
    """
    content = normalise_content(write.content)
    if not content:
        raise UnprocessableError("Memory content must not be empty")

    verdict = inspect_sensitivity(content)
    if verdict.sensitive:
        raise MemorySensitivityError(
            f"Refusing to store memory: {verdict.reason}. "
            "Store a reference to where the credential lives instead."
        )

    if write.layer in _PROJECT_SCOPED and write.project_id is None:
        raise UnprocessableError("Project memory must be attached to a project")

    settings = get_settings()
    ttl_days = write.ttl_days
    if ttl_days is None and write.layer is MemoryLayer.WORKING:
        ttl_days = 1
    ttl_days = ttl_days or settings.memory_retention_days

    dedup_key = write.dedup_key or default_dedup_key(write)
    existing = (
        (
            await db.execute(
                select(Memory).where(Memory.dedup_key == dedup_key).order_by(Memory.version.desc())
            )
        )
        .scalars()
        .first()
    )

    now = datetime.now(UTC)
    parent_id: UUID | None = write.parent_memory_id
    if existing is not None:
        if existing.content_hash == content_digest(content):
            existing.access_count += 1
            existing.accessed_at = now
            existing.expires_at = now + timedelta(days=ttl_days)
            await db.flush()
            return existing
        # Same key, new content: supersede rather than mutate.
        version = existing.version + 1
        parent_id = existing.id
    else:
        version = 1

    embedding_vector, embedding_model = await _embed(db, content, enabled=write.embed)
    memory = Memory(
        layer=write.layer,
        agent_id=write.agent_id,
        project_id=write.project_id,
        task_id=write.task_id,
        user_id=write.user_id,
        parent_memory_id=parent_id,
        key=write.key,
        content=content,
        summary=(write.summary or content[:400]).strip(),
        embedding=embedding_vector,
        embedding_vec=embedding_vector,
        embedding_model=embedding_model,
        embedding_dim=len(embedding_vector) if embedding_vector else None,
        source=write.source,
        source_ref=write.source_ref,
        content_hash=content_digest(content),
        dedup_key=dedup_key,
        importance=_clamp(write.importance),
        confidence=_clamp(write.confidence),
        version=version,
        is_sensitive=False,
        expires_at=now + timedelta(days=ttl_days),
        accessed_at=now,
        access_count=0,
        meta=write.meta,
    )
    db.add(memory)
    await db.flush()
    logger.info(
        "memory_written",
        layer=memory.layer.value,
        version=memory.version,
        embedded=embedding_vector is not None,
    )
    return memory


async def _embed(
    db: AsyncSession, content: str, *, enabled: bool
) -> tuple[list[float] | None, str | None]:
    if not enabled:
        return None, None
    embedder = await resolve_embedder(db)
    if embedder is None:
        return None, None
    try:
        vectors = await embedder.embed([content])
    except Exception as exc:
        # A failing embedder must not block memory writes; the record is still
        # useful through lexical retrieval.
        logger.warning("memory_embedding_failed", error=str(exc))
        return None, None
    return vectors[0], embedder.model


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


# ------------------------------------------------------------------ retrieval


def visibility_filter(
    *,
    agent_id: UUID | None,
    project_id: UUID | None,
    task_id: UUID | None = None,
    user_id: UUID | None = None,
    agent_scope: bool = True,
) -> Any:
    """Build the WHERE clause describing which memories may be read.

    Every scope that was set when the memory was written is required again when
    it is read, so a narrower scope can never be widened by a reader:

      * a memory tagged with an agent is private to that agent;
      * a memory tagged with a project is private to that project;
      * a memory tagged with a task is private to that task;
      * a memory tagged with a user is private to that user.

    Rows with the tag unset are shared, which is what makes semantic knowledge
    written by the platform itself reachable by every agent.

    ``agent_scope=False`` skips the agent clause, for an operator-facing listing
    that shows an agent's memories. It never skips user scoping.
    """
    clauses: list[Any] = []

    if agent_scope:
        clauses.append(
            or_(
                Memory.agent_id.is_(None),
                Memory.agent_id == agent_id if agent_id is not None else false(),
            )
        )

    project_clause: list[Any] = [Memory.project_id.is_(None)]
    if project_id is not None:
        project_clause.append(Memory.project_id == project_id)
    clauses.append(or_(*project_clause))

    task_clause: list[Any] = [Memory.task_id.is_(None)]
    if task_id is not None:
        task_clause.append(Memory.task_id == task_id)
    clauses.append(
        or_(
            Memory.layer.not_in([MemoryLayer.WORKING.value, MemoryLayer.CONVERSATION.value]),
            or_(*task_clause),
        )
    )

    user_clause: list[Any] = [Memory.user_id.is_(None)]
    if user_id is not None:
        user_clause.append(Memory.user_id == user_id)
    clauses.append(or_(*user_clause))

    statement: Any = Memory.expires_at.is_(None) | (Memory.expires_at > datetime.now(UTC))
    for clause in clauses:
        statement = statement & clause
    return statement


async def retrieve(
    db: AsyncSession,
    *,
    query: str,
    agent_id: UUID | None = None,
    project_id: UUID | None = None,
    task_id: UUID | None = None,
    user_id: UUID | None = None,
    layers: list[MemoryLayer] | None = None,
    limit: int | None = None,
    min_score: float = 0.05,
    weights: ScoreWeights = DEFAULT_WEIGHTS,
    use_vector: bool = True,
) -> list[MemoryHit]:
    """Rank the memories relevant to ``query`` within the caller's scope."""
    settings = get_settings()
    limit = limit or settings.memory_max_items

    statement: Select[tuple[Memory]] = select(Memory).where(
        visibility_filter(
            agent_id=agent_id, project_id=project_id, task_id=task_id, user_id=user_id
        )
    )
    if layers:
        statement = statement.where(Memory.layer.in_([layer.value for layer in layers]))
    statement = statement.order_by(Memory.created_at.desc()).limit(500)

    candidates = list((await db.execute(statement)).scalars().all())
    if not candidates:
        return []

    query_tokens = tokenize(query)
    query_vector: list[float] | None = None
    if use_vector and query_tokens:
        embedder = await resolve_embedder(db)
        if embedder is not None:
            try:
                query_vector = (await embedder.embed([query], input_type="query"))[0]
            except Exception as exc:
                logger.warning("memory_query_embedding_failed", error=str(exc))

    now = datetime.now(UTC)
    hits: list[MemoryHit] = []
    for memory in candidates:
        tokens = tokenize(f"{memory.content} {memory.key or ''}")
        lexical = lexical_score(_counter(query_tokens), tokens)
        age = (now - _as_utc(memory.created_at)).total_seconds()
        recency = recency_score(age, settings.memory_half_life_days)
        usage = usage_score(memory.access_count)
        vector: float | None = None
        if query_vector is not None and memory.embedding:
            vector = _cosine(query_vector, memory.embedding)
        score = combine(
            lexical=lexical,
            recency=recency,
            importance=_clamp(memory.importance),
            usage=usage,
            vector=vector,
            weights=weights,
            layer_boost=_layer_boost(memory.layer),
        )
        if score >= min_score:
            hits.append(
                MemoryHit(
                    memory=memory,
                    score=score,
                    lexical=lexical,
                    recency=recency,
                    usage=usage,
                    vector=vector,
                )
            )

    hits.sort(key=lambda hit: hit.score, reverse=True)
    return hits[:limit]


def _counter(tokens: list[str]) -> Any:
    from collections import Counter

    return Counter(tokens)


def _layer_boost(layer: MemoryLayer) -> float:
    return {
        MemoryLayer.WORKING: 0.05,
        MemoryLayer.CONVERSATION: 0.04,
        MemoryLayer.EPISODIC: 0.02,
        MemoryLayer.PROJECT: 0.03,
        MemoryLayer.AGENT: 0.02,
        MemoryLayer.SEMANTIC: 0.0,
    }.get(layer, 0.0)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _cosine(left: list[float], right: list[float]) -> float:
    from app.memory.embeddings import cosine_similarity

    return cosine_similarity(left, right)


async def mark_accessed(db: AsyncSession, memories: list[Memory]) -> None:
    """Record that these memories were used, so ranking improves over time."""
    if not memories:
        return
    now = datetime.now(UTC)
    for memory in memories:
        memory.access_count += 1
        memory.accessed_at = now
    await db.flush()


async def build_context(
    db: AsyncSession,
    *,
    query: str,
    agent_id: UUID | None = None,
    project_id: UUID | None = None,
    task_id: UUID | None = None,
    user_id: UUID | None = None,
    max_chars: int | None = None,
    limit: int | None = None,
) -> str:
    """Render the most relevant memories as a prompt section.

    Returns an empty string when nothing is relevant — callers must not inject
    a "no memories" placeholder, which costs tokens and teaches the model
    nothing.
    """
    settings = get_settings()
    max_chars = max_chars or settings.memory_max_context_chars
    hits = await retrieve(
        db,
        query=query,
        agent_id=agent_id,
        project_id=project_id,
        task_id=task_id,
        user_id=user_id,
        limit=limit,
    )
    if not hits:
        return ""

    lines = ["## Relevant memory", ""]
    used = 0
    consumed: list[Memory] = []
    for hit in hits:
        body = hit.memory.summary or hit.memory.content
        entry = f"- ({hit.memory.layer.value}) {body.strip()}"
        if used + len(entry) > max_chars:
            break
        lines.append(entry)
        used += len(entry)
        consumed.append(hit.memory)
    if len(lines) == 2:
        return ""
    await mark_accessed(db, consumed)
    return "\n".join(lines)


# -------------------------------------------------------------------- summary
def summarize_memories(memories: list[Memory], *, max_chars: int = 1200) -> str:
    """Deterministic extractive summary of a memory set.

    Deliberately not a model call: summarisation runs inside request paths, and
    a deterministic summary cannot hallucinate. Longer memories are truncated at
    a sentence boundary.
    """
    if not memories:
        return ""
    entries: list[str] = []
    per_item = max(80, max_chars // max(1, len(memories)))
    for memory in memories:
        body = (memory.summary or memory.content).strip()
        if len(body) > per_item:
            cut = body[:per_item]
            boundary = max(cut.rfind(". "), cut.rfind("\n"))
            body = (cut[: boundary + 1] if boundary > 40 else cut).strip() + "…"
        entries.append(f"[{memory.layer.value}] {body}")
    return "\n".join(entries)[:max_chars]


# ----------------------------------------------------------------- maintenance
async def history_for_key(db: AsyncSession, dedup_key: str) -> list[Memory]:
    """Every version of a keyed memory, oldest first."""
    rows = await db.execute(
        select(Memory).where(Memory.dedup_key == dedup_key).order_by(Memory.version.asc())
    )
    return list(rows.scalars().all())


async def purge_expired(db: AsyncSession, *, batch: int = 500) -> int:
    """Delete expired memories. Returns the number removed."""
    cutoff = datetime.now(UTC)
    rows = await db.execute(
        select(Memory)
        .where(Memory.expires_at.is_not(None), Memory.expires_at <= cutoff)
        .limit(batch)
    )
    memories = list(rows.scalars().all())
    for memory in memories:
        await db.delete(memory)
    await db.flush()
    if memories:
        logger.info("memory_purged", count=len(memories))
    return len(memories)


async def delete_memory(db: AsyncSession, memory_id: UUID) -> None:
    memory = await db.get(Memory, memory_id)
    if memory is None:
        raise NotFoundError("Memory not found")
    await db.delete(memory)
    await db.flush()


async def list_memories(
    db: AsyncSession,
    *,
    layer: MemoryLayer | None = None,
    agent_id: UUID | None = None,
    project_id: UUID | None = None,
    task_id: UUID | None = None,
    user_id: UUID | None = None,
    query: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Memory], int]:
    """Browse memories for the operator UI, scoped to the caller's user.

    The listing deliberately ignores agent privacy (an operator sees what the
    platform's agents wrote) but never crosses a user boundary.
    """
    statement: Select[tuple[Memory]] = select(Memory).where(
        visibility_filter(agent_id=None, project_id=project_id, user_id=user_id, agent_scope=False)
    )
    if layer is not None:
        statement = statement.where(Memory.layer == layer.value)
    if agent_id is not None:
        statement = statement.where(Memory.agent_id == agent_id)
    if project_id is not None:
        statement = statement.where(Memory.project_id == project_id)
    if task_id is not None:
        statement = statement.where(Memory.task_id == task_id)
    if query:
        pattern = f"%{query.lower()}%"
        statement = statement.where(func.lower(Memory.content).like(pattern))

    total = (await db.execute(select(func.count()).select_from(statement.subquery()))).scalar_one()
    rows = (
        (await db.execute(statement.order_by(Memory.created_at.desc()).limit(limit).offset(offset)))
        .scalars()
        .all()
    )
    return list(rows), int(total)


async def vector_search(
    db: AsyncSession,
    *,
    query: str,
    limit: int = 10,
    project_id: UUID | None = None,
    agent_id: UUID | None = None,
) -> list[MemoryHit]:
    """ANN search straight from pgvector, for callers that need it.

    Falls back to :func:`retrieve` when no embedder is configured, so the
    endpoint behaves consistently rather than erroring.
    """
    embedder = await resolve_embedder(db)
    if embedder is None:
        return await retrieve(
            db, query=query, project_id=project_id, agent_id=agent_id, limit=limit
        )
    try:
        vector = (await embedder.embed([query], input_type="query"))[0]
    except Exception as exc:
        logger.warning("vector_search_embedding_failed", error=str(exc))
        return await retrieve(
            db, query=query, project_id=project_id, agent_id=agent_id, limit=limit
        )

    statement = (
        select(Memory, Memory.embedding_vec.cosine_distance(vector).label("distance"))
        .where(
            visibility_filter(agent_id=agent_id, project_id=project_id, task_id=None, user_id=None),
            Memory.embedding_vec.is_not(None),
        )
        .order_by(text("distance"))
        .limit(limit)
    )
    rows = (await db.execute(statement)).all()
    hits: list[MemoryHit] = []
    for memory, distance in rows:
        similarity = 1.0 - float(distance)
        hits.append(
            MemoryHit(
                memory=memory,
                score=similarity,
                lexical=0.0,
                recency=recency_score(
                    (datetime.now(UTC) - _as_utc(memory.created_at)).total_seconds(),
                    get_settings().memory_half_life_days,
                ),
                usage=usage_score(memory.access_count),
                vector=similarity,
            )
        )
    return hits


async def record_task_outcome(db: AsyncSession, task: Task) -> Memory | None:
    """Write an episodic memory describing a finished task.

    Only terminal, non-empty outcomes are recorded; a failed run with no output
    is noise, not experience.
    """
    if task.status.value not in {"completed", "failed", "cancelled"}:
        return None
    if task.status.value != "completed":
        return None
    output = task.output or {}
    content = str(output.get("content") or "").strip()
    if not content:
        return None

    summary = f"Task '{task.title}' completed. Outcome: {content[:600]}"
    return await write_memory(
        db,
        MemoryWrite(
            content=summary,
            layer=MemoryLayer.EPISODIC,
            agent_id=task.assigned_agent_id,
            project_id=task.project_id,
            task_id=task.id,
            key=f"task:{task.id}",
            source="task_outcome",
            source_ref=str(task.id),
            importance=0.6,
            confidence=0.8,
            meta={"title": task.title, "status": task.status.value},
        ),
    )
