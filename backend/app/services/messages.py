"""Agent-to-agent messaging.

Specialists report to the orchestrator through ``agent_messages``: each
delegated step leaves a ``handoff`` message carrying the child's result, and
the orchestrator's own notes are appended as ``status`` messages. Keeping these
in the database (rather than only in an in-memory list) is what makes a resumed
orchestration able to rebuild its context after a restart.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import UnprocessableError
from app.db.models import Agent, AgentMessage

MAX_CONTENT_CHARS = 20_000


async def record_message(
    db: AsyncSession,
    *,
    task_id: UUID | None,
    from_agent_id: UUID | None,
    to_agent_id: UUID | None,
    content: str,
    message_type: str = "message",
    payload: dict[str, Any] | None = None,
    conversation_id: UUID | None = None,
) -> AgentMessage:
    if from_agent_id is not None and to_agent_id is not None and from_agent_id == to_agent_id:
        raise UnprocessableError("An agent cannot message itself")

    message = AgentMessage(
        task_id=task_id,
        conversation_id=conversation_id,
        from_agent_id=from_agent_id,
        to_agent_id=to_agent_id,
        message_type=message_type[:40],
        content=content[:MAX_CONTENT_CHARS],
        payload=payload or {},
    )
    db.add(message)
    await db.flush()
    return message


async def list_messages(
    db: AsyncSession,
    *,
    task_id: UUID | None = None,
    agent_id: UUID | None = None,
    limit: int = 100,
) -> list[AgentMessage]:
    statement = select(AgentMessage).order_by(AgentMessage.created_at.asc()).limit(limit)
    if task_id is not None:
        statement = statement.where(AgentMessage.task_id == task_id)
    if agent_id is not None:
        statement = statement.where(
            (AgentMessage.from_agent_id == agent_id) | (AgentMessage.to_agent_id == agent_id)
        )
    result = await db.execute(statement)
    return list(result.scalars())


async def transcript(db: AsyncSession, *, task_id: UUID, limit: int = 50) -> list[dict[str, Any]]:
    """Readable transcript of a task's agent conversation, newest last."""
    messages = await list_messages(db, task_id=task_id, limit=limit)
    slugs = {
        agent_id
        for message in messages
        for agent_id in (message.from_agent_id, message.to_agent_id)
        if agent_id is not None
    }
    names: dict[UUID, str] = {}
    if slugs:
        result = await db.execute(select(Agent.id, Agent.slug).where(Agent.id.in_(slugs)))
        names = {row[0]: row[1] for row in result}

    return [
        {
            "id": str(message.id),
            "type": message.message_type,
            "from": names.get(message.from_agent_id) if message.from_agent_id else None,
            "to": names.get(message.to_agent_id) if message.to_agent_id else None,
            "content": message.content,
            "payload": message.payload,
            "created_at": message.created_at.isoformat(),
        }
        for message in messages
    ]
