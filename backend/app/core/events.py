"""Event definitions and the Redis-backed event bus.

Events are published twice: to a capped Redis Stream for durable replay and to
a Redis Pub/Sub channel for low-latency fan-out to WebSocket/SSE clients.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, cast
from uuid import UUID, uuid4

import redis.asyncio as aioredis
from pydantic import BaseModel, Field

DEFAULT_CHANNEL = "agentos:events"
DEFAULT_STREAM = "agentos:events:stream"


class EventType(StrEnum):
    TASK_CREATED = "task.created"
    TASK_STARTED = "task.started"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    TASK_CANCELLED = "task.cancelled"
    TASK_PROGRESS = "task.progress"
    AGENT_CREATED = "agent.created"
    AGENT_STARTED = "agent.started"
    AGENT_STOPPED = "agent.stopped"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    FINDING_CREATED = "finding.created"
    APPROVAL_REQUIRED = "approval.required"
    APPROVAL_DECIDED = "approval.decided"
    SUBMISSION_COMPLETED = "submission.completed"
    MODEL_USAGE = "model.usage"
    SYSTEM_ALERT = "system.alert"


class Event(BaseModel):
    """An immutable domain event."""

    id: UUID = Field(default_factory=uuid4)
    type: EventType
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    source: str = "agentos"
    task_id: UUID | None = None
    agent_id: UUID | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    def to_json(self) -> str:
        return self.model_dump_json()


class RedisEventBus:
    """Publish domain events to Redis Streams and Pub/Sub."""

    def __init__(
        self,
        url: str,
        *,
        socket_timeout: float = 5.0,
        channel: str = DEFAULT_CHANNEL,
        stream: str = DEFAULT_STREAM,
        maxlen: int = 20_000,
    ) -> None:
        self._channel = channel
        self._stream = stream
        self._maxlen = maxlen
        self._client: aioredis.Redis = aioredis.Redis.from_url(
            url, socket_timeout=socket_timeout, decode_responses=True
        )

    async def ping(self) -> bool:
        return bool(await cast("Any", self._client.ping()))

    async def publish(self, event: Event) -> str:
        """Persist the event in the stream and notify subscribers.

        Returns the Redis Stream entry id.
        """
        body = event.to_json()
        stream_id: str = await self._client.xadd(
            self._stream, {"event": body}, maxlen=self._maxlen, approximate=True
        )
        await self._client.publish(self._channel, body)
        return stream_id

    async def recent(self, count: int = 50) -> list[Event]:
        """Return the most recent events, oldest first."""
        entries = await self._client.xrevrange(self._stream, count=max(1, min(count, 500)))
        events: list[Event] = []
        for _entry_id, fields in reversed(entries):
            raw = fields.get("event")
            if raw:
                events.append(Event.model_validate(json.loads(raw)))
        return events

    async def close(self) -> None:
        await self._client.aclose()

    @property
    def client(self) -> aioredis.Redis:
        return self._client

    @property
    def channel(self) -> str:
        return self._channel

    @property
    def stream(self) -> str:
        return self._stream
