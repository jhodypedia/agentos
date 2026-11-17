"""Memory endpoints: write, read, search, summarise, purge."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AdminPrincipal, DbSession, OperatorPrincipal
from app.core.config import get_settings
from app.core.exceptions import NotFoundError, UnprocessableError
from app.db.enums import MemoryLayer
from app.db.models import Memory
from app.memory.sensitivity import inspect as inspect_sensitivity
from app.schemas.memory import (
    MemoryContextResponse,
    MemoryCreate,
    MemoryPublic,
    MemoryScore,
    MemorySearchHit,
    MemorySearchRequest,
    MemorySummaryResponse,
    MemoryUpdate,
)
from app.services import memory as memory_service
from app.services.audit import record_audit

router = APIRouter(prefix="/memory", tags=["memory"])


async def _get_memory(db: AsyncSession, memory_id: UUID) -> Memory:
    memory = await db.get(Memory, memory_id)
    if memory is None:
        raise NotFoundError("Memory not found")
    return memory


def _hit(hit: memory_service.MemoryHit) -> MemorySearchHit:
    ranking = hit.explain()
    return MemorySearchHit(
        memory=MemoryPublic.model_validate(hit.memory),
        ranking=MemoryScore(
            score=ranking["score"],
            lexical=ranking["lexical"],
            recency=ranking["recency"],
            usage=ranking["usage"],
            vector=ranking["vector"],
        ),
    )


@router.get("", response_model=list[MemoryPublic], summary="List memory records")
async def list_memories(
    principal: OperatorPrincipal,
    db: DbSession,
    layer: MemoryLayer | None = Query(default=None),
    agent_id: UUID | None = Query(default=None),
    project_id: UUID | None = Query(default=None),
    task_id: UUID | None = Query(default=None),
    q: str | None = Query(default=None, max_length=500),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[MemoryPublic]:
    memories, _total = await memory_service.list_memories(
        db,
        layer=layer,
        agent_id=agent_id,
        project_id=project_id,
        task_id=task_id,
        user_id=principal.user.id,
        query=q,
        limit=limit,
        offset=offset,
    )
    return [MemoryPublic.model_validate(memory) for memory in memories]


@router.get("/layers", response_model=list[str], summary="List memory layers")
async def list_layers(principal: OperatorPrincipal) -> list[str]:
    return [layer.value for layer in MemoryLayer]


@router.post(
    "",
    response_model=MemoryPublic,
    status_code=status.HTTP_201_CREATED,
    summary="Store a memory",
)
async def create_memory(
    payload: MemoryCreate,
    principal: OperatorPrincipal,
    db: DbSession,
    request: Request,
) -> MemoryPublic:
    memory = await memory_service.write_memory(
        db,
        memory_service.MemoryWrite(
            content=payload.content,
            layer=payload.layer,
            agent_id=payload.agent_id,
            project_id=payload.project_id,
            task_id=payload.task_id,
            user_id=principal.user.id,
            key=payload.key,
            summary=payload.summary,
            importance=payload.importance,
            confidence=payload.confidence,
            ttl_days=payload.ttl_days,
            meta=payload.meta,
            source="api",
        ),
    )
    await record_audit(
        db,
        action="memory.created",
        resource_type="memory",
        resource_id=memory.id,
        actor_user_id=principal.user.id,
        request=request,
        meta={"layer": memory.layer.value, "version": memory.version},
    )
    await db.commit()
    await db.refresh(memory)
    return MemoryPublic.model_validate(memory)


@router.post("/search", response_model=list[MemorySearchHit], summary="Rank memories for a query")
async def search_memories(
    payload: MemorySearchRequest, principal: OperatorPrincipal, db: DbSession
) -> list[MemorySearchHit]:
    hits = await memory_service.retrieve(
        db,
        query=payload.query,
        agent_id=payload.agent_id,
        project_id=payload.project_id,
        task_id=payload.task_id,
        user_id=principal.user.id,
        layers=payload.layers,
        limit=payload.limit,
        use_vector=payload.use_vector,
    )
    await memory_service.mark_accessed(db, [hit.memory for hit in hits])
    await db.commit()
    for hit in hits:
        # ``mark_accessed`` bumped the counter server-side; reload so the
        # response shows the new access_count instead of an expired attribute.
        await db.refresh(hit.memory)
    return [_hit(hit) for hit in hits]


@router.post(
    "/context", response_model=MemoryContextResponse, summary="Render a memory context block"
)
async def memory_context(
    payload: MemorySearchRequest, principal: OperatorPrincipal, db: DbSession
) -> MemoryContextResponse:
    settings = get_settings()
    context = await memory_service.build_context(
        db,
        query=payload.query,
        agent_id=payload.agent_id,
        project_id=payload.project_id,
        task_id=payload.task_id,
        user_id=principal.user.id,
        limit=payload.limit,
        max_chars=settings.memory_max_context_chars,
    )
    await db.commit()
    items = sum(1 for line in context.splitlines() if line.startswith("- ("))
    return MemoryContextResponse(query=payload.query, context=context, items=items)


@router.post(
    "/summary", response_model=MemorySummaryResponse, summary="Summarise matching memories"
)
async def summarise_memories(
    payload: MemorySearchRequest, principal: OperatorPrincipal, db: DbSession
) -> MemorySummaryResponse:
    hits = await memory_service.retrieve(
        db,
        query=payload.query,
        agent_id=payload.agent_id,
        project_id=payload.project_id,
        task_id=payload.task_id,
        user_id=principal.user.id,
        layers=payload.layers,
        limit=payload.limit,
    )
    memories = [hit.memory for hit in hits]
    return MemorySummaryResponse(
        summary=memory_service.summarize_memories(memories), items=len(memories)
    )


@router.get("/{memory_id}", response_model=MemoryPublic, summary="Get a memory")
async def get_memory(memory_id: UUID, principal: OperatorPrincipal, db: DbSession) -> MemoryPublic:
    memory = await _get_memory(db, memory_id)
    return MemoryPublic.model_validate(memory)


@router.get("/{memory_id}/versions", response_model=list[MemoryPublic], summary="Version history")
async def memory_versions(
    memory_id: UUID, principal: OperatorPrincipal, db: DbSession
) -> list[MemoryPublic]:
    memory = await _get_memory(db, memory_id)
    if not memory.dedup_key:
        return [MemoryPublic.model_validate(memory)]
    history = await memory_service.history_for_key(db, memory.dedup_key)
    return [MemoryPublic.model_validate(item) for item in history]


@router.patch("/{memory_id}", response_model=MemoryPublic, summary="Update a memory")
async def update_memory(
    memory_id: UUID,
    payload: MemoryUpdate,
    principal: OperatorPrincipal,
    db: DbSession,
    request: Request,
) -> MemoryPublic:
    """Update in place, or supersede with a new version when content changes.

    Changing content through this endpoint creates a new version rather than
    rewriting history; only metadata (importance, confidence, TTL, summary) is
    updated in place.
    """
    memory = await _get_memory(db, memory_id)

    if payload.content is not None and payload.content != memory.content:
        verdict = inspect_sensitivity(payload.content)
        if verdict.sensitive:
            raise memory_service.MemorySensitivityError(
                f"Refusing to store memory: {verdict.reason}"
            )
        updated = await memory_service.write_memory(
            db,
            memory_service.MemoryWrite(
                content=payload.content,
                layer=memory.layer,
                agent_id=memory.agent_id,
                project_id=memory.project_id,
                task_id=memory.task_id,
                user_id=memory.user_id,
                key=memory.key,
                summary=payload.summary or memory.summary,
                source=memory.source,
                source_ref=memory.source_ref,
                importance=(
                    payload.importance if payload.importance is not None else memory.importance
                ),
                confidence=(
                    payload.confidence if payload.confidence is not None else memory.confidence
                ),
                dedup_key=memory.dedup_key,
                parent_memory_id=memory.id,
                meta=memory.meta,
                ttl_days=payload.ttl_days,
            ),
        )
        await db.commit()
        await db.refresh(updated)
        return MemoryPublic.model_validate(updated)

    if payload.summary is not None:
        memory.summary = payload.summary
    if payload.importance is not None:
        memory.importance = payload.importance
    if payload.confidence is not None:
        memory.confidence = payload.confidence
    if payload.ttl_days is not None:
        memory.expires_at = datetime.now(UTC) + timedelta(days=payload.ttl_days)
    if not memory.summary:
        memory.summary = memory.content[:400]
    if payload.content is not None and not payload.content.strip():
        raise UnprocessableError("Memory content must not be empty")
    await db.flush()
    await record_audit(
        db,
        action="memory.updated",
        resource_type="memory",
        resource_id=memory.id,
        actor_user_id=principal.user.id,
        request=request,
        meta={"layer": memory.layer.value},
    )
    await db.commit()
    await db.refresh(memory)
    return MemoryPublic.model_validate(memory)


@router.delete("/{memory_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a memory")
async def delete_memory(
    memory_id: UUID, principal: AdminPrincipal, db: DbSession, request: Request
) -> None:
    await memory_service.delete_memory(db, memory_id)
    await record_audit(
        db,
        action="memory.deleted",
        resource_type="memory",
        resource_id=memory_id,
        actor_user_id=principal.user.id,
        request=request,
    )
    await db.commit()


@router.post("/purge-expired", response_model=dict[str, int], summary="Delete expired memories")
async def purge_expired(
    principal: AdminPrincipal, db: DbSession, request: Request
) -> dict[str, int]:
    removed = await memory_service.purge_expired(db)
    await record_audit(
        db,
        action="memory.purged",
        resource_type="memory",
        actor_user_id=principal.user.id,
        request=request,
        meta={"removed": removed},
    )
    await db.commit()
    return {"removed": removed}
