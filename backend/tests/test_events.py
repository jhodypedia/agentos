from __future__ import annotations

import json
from uuid import uuid4

import pytest

from app.core.events import Event, EventType, RedisEventBus


def test_event_serialisation() -> None:
    task_id = uuid4()
    event = Event(type=EventType.TASK_CREATED, task_id=task_id, payload={"title": "x"})
    decoded = Event.model_validate(json.loads(event.to_json()))
    assert decoded.id == event.id
    assert decoded.type is EventType.TASK_CREATED
    assert decoded.task_id == task_id


@pytest.mark.integration
@pytest.mark.redis
async def test_event_bus_publish_and_read() -> None:
    from app.core.config import get_settings

    bus = RedisEventBus(get_settings().redis_url, stream="agentos:test:stream")
    try:
        event = Event(type=EventType.AGENT_STARTED, payload={"agent": "test"})
        stream_id = await bus.publish(event)
        assert stream_id
        recent = await bus.recent(10)
        assert any(item.id == event.id for item in recent)
    finally:
        await bus.client.delete("agentos:test:stream")
        await bus.close()
