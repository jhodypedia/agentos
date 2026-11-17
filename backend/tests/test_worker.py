"""End-to-end worker test: a queued task really runs and completes.

This drives the whole path the way production does — enqueue a task, let the
worker claim it, execute through the real runtime against a scripted provider,
and persist the outcome — rather than calling the runtime directly. It is the
test that would catch a broken claim, a missing session factory or an
unreachable provider wiring.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.db.enums import TaskStatus
from app.db.models import Agent, Task
from app.db.session import get_session_factory
from app.providers.base import LLMRequest, LLMResponse, Usage
from app.worker import Worker

pytestmark = pytest.mark.integration


class ScriptedProvider:
    def __init__(self, *responses: LLMResponse) -> None:
        self.queue = list(responses)
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if not self.queue:
            raise AssertionError("ScriptedProvider ran out of responses")
        return self.queue.pop(0)

    async def aclose(self) -> None:
        return None


def _reply(text: str) -> LLMResponse:
    return LLMResponse(
        content=text,
        usage=Usage(prompt_tokens=12, completion_tokens=8, total_tokens=20),
        finish_reason="stop",
        model="fake-model",
    )


@pytest.fixture(autouse=True)
async def seeded() -> AsyncIterator[None]:
    from app.orchestrator.specialists import sync_specialists
    from app.services.agents import sync_tool_registry
    from app.tools.builtin import BUILTIN_TOOLS

    factory = get_session_factory()
    async with factory() as session:
        await sync_tool_registry(session, [tool() for tool in BUILTIN_TOOLS])
        await sync_specialists(session)
        await session.commit()
    yield


async def _queued_task() -> Task:
    factory = get_session_factory()
    async with factory() as db:
        agent = await db.scalar(select(Agent).where(Agent.slug == "main-orchestrator").limit(1))
        assert agent is not None
        # The specialist is seeded without a model; assigning one keeps the test
        # from depending on an operator having configured a provider.
        agent.model_name = "fake-model"
        agent.model_provider_id = None
        await db.commit()
        task = Task(
            title="worker end to end",
            objective="Summarise the situation",
            status=TaskStatus.PENDING,
            assigned_agent_id=agent.id,
            max_attempts=1,
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)
        return task


async def test_worker_claims_and_completes_a_task(monkeypatch: pytest.MonkeyPatch) -> None:
    task = await _queued_task()
    provider = ScriptedProvider(_reply("All clear."))

    # The provider comes from the agent's record, so patch the resolver instead
    # of the provider class: that keeps the real execution path intact.
    from app.services import execution as execution_service

    async def resolver(db: object, agent: object) -> object:
        return provider

    monkeypatch.setattr(execution_service, "_resolve_provider", resolver)

    worker = Worker(get_settings(), name="test-worker", lease_seconds=60)
    worker._factory = get_session_factory(get_settings())
    assert await worker.run_once() is True

    factory = get_session_factory()
    async with factory() as db:
        stored = await db.get(Task, task.id)
        assert stored is not None
        assert stored.status == TaskStatus.COMPLETED, stored.error
        assert stored.output["content"] == "All clear."
        assert stored.used_tokens == 20
        assert stored.claimed_by == "test-worker"
    assert provider.requests, "the runtime never called the provider"


async def test_worker_run_once_returns_false_on_an_empty_queue() -> None:
    worker = Worker(get_settings(), name="idle-worker", lease_seconds=60)
    worker._factory = get_session_factory(get_settings())
    assert await worker.run_once() is False


async def test_worker_marks_a_crashed_run_for_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import execution as execution_service

    task = await _queued_task()

    async def boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(execution_service, "execute_task", boom)

    worker = Worker(get_settings(), name="crashy-worker", lease_seconds=60)
    worker._factory = get_session_factory(get_settings())
    await worker.run_once()

    factory = get_session_factory()
    async with factory() as db:
        stored = await db.get(Task, task.id)
        assert stored is not None
        assert stored.claimed_by is None
        assert "provider exploded" in (stored.error or "")


async def test_task_duration_is_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    """A completed task must leave a duration observation behind, or a worker's
    throughput is invisible to a dashboard."""
    from app.core.metrics import TASK_DURATION
    from app.services import execution as execution_service

    await _queued_task()
    provider = ScriptedProvider(_reply("All clear."))

    async def resolver(db: object, agent: object) -> object:
        return provider

    monkeypatch.setattr(execution_service, "_resolve_provider", resolver)

    worker = Worker(get_settings(), name="duration-worker", lease_seconds=60)
    worker._factory = get_session_factory(get_settings())
    assert await worker.run_once() is True

    rendered = "\n".join(TASK_DURATION.render())
    assert 'agentos_task_duration_seconds_count{worker="duration-worker"}' in rendered
    assert 'agentos_task_duration_seconds_sum{worker="duration-worker"}' in rendered
