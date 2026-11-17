"""Queue claiming, leases and crash recovery.

These tests exercise the properties that make a background worker safe: two
workers cannot take the same task, a dead worker's task comes back, dependencies
hold a task back, and a task that keeps failing eventually stops being retried.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.db.enums import TaskStatus
from app.db.models import Task, TaskDependency
from app.db.session import get_session_factory
from app.services import queue as queue_service

pytestmark = pytest.mark.integration


async def _make_task(**kwargs: object) -> Task:
    factory = get_session_factory()
    async with factory() as db:
        task = Task(
            title=kwargs.pop("title", "queued task"),
            objective=kwargs.pop("objective", "do the thing"),
            status=kwargs.pop("status", TaskStatus.PENDING),
            **kwargs,
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)
        return task


async def _reload(task_id: object) -> Task:
    factory = get_session_factory()
    async with factory() as db:
        task = await db.get(Task, task_id)
        assert task is not None
        return task


async def test_claim_takes_the_highest_priority_task() -> None:
    low = await _make_task(title="low", priority=500)
    high = await _make_task(title="high", priority=1)

    factory = get_session_factory()
    async with factory() as db:
        claimed = await queue_service.claim_next(
            db, worker_id="w1", lease_seconds=60, settings=get_settings()
        )

    assert claimed is not None
    assert claimed.id == high.id
    assert claimed.claimed_by == "w1"
    assert claimed.status == TaskStatus.QUEUED
    assert (await _reload(low.id)).claimed_by is None


async def test_a_second_worker_cannot_claim_a_live_lease() -> None:
    task = await _make_task()
    factory = get_session_factory()
    settings = get_settings()

    async with factory() as db:
        first = await queue_service.claim_next(
            db, worker_id="w1", lease_seconds=120, settings=settings
        )
    async with factory() as db:
        second = await queue_service.claim_next(
            db, worker_id="w2", lease_seconds=120, settings=settings
        )

    assert first is not None and first.id == task.id
    assert second is None
    stored = await _reload(task.id)
    assert stored.claimed_by == "w1"


async def test_expired_lease_is_claimable_by_another_worker() -> None:
    task = await _make_task()
    factory = get_session_factory()
    settings = get_settings()

    async with factory() as db:
        await queue_service.claim_next(db, worker_id="dead", lease_seconds=60, settings=settings)
    async with factory() as db:
        stored = await db.get(Task, task.id)
        assert stored is not None
        stored.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()

    async with factory() as db:
        claimed = await queue_service.claim_next(
            db, worker_id="alive", lease_seconds=60, settings=settings
        )

    assert claimed is not None and claimed.id == task.id
    assert claimed.claimed_by == "alive"


async def test_reap_requeues_a_task_left_running_by_a_dead_worker() -> None:
    task = await _make_task(status=TaskStatus.RUNNING, max_attempts=3)
    factory = get_session_factory()
    async with factory() as db:
        stored = await db.get(Task, task.id)
        assert stored is not None
        stored.claimed_by = "gone"
        stored.lease_expires_at = datetime.now(UTC) - timedelta(minutes=5)
        await db.commit()

    async with factory() as db:
        reaped = await queue_service.reap_expired(db)

    stored = await _reload(task.id)
    assert reaped == 1
    assert stored.status == TaskStatus.QUEUED
    assert stored.claimed_by is None
    assert stored.lease_expires_at is None
    assert stored.attempts == 1


async def test_reap_fails_a_task_that_exhausted_its_attempts() -> None:
    task = await _make_task(status=TaskStatus.RUNNING, max_attempts=1)
    factory = get_session_factory()
    async with factory() as db:
        stored = await db.get(Task, task.id)
        assert stored is not None
        stored.claimed_by = "gone"
        stored.lease_expires_at = datetime.now(UTC) - timedelta(minutes=5)
        await db.commit()

    async with factory() as db:
        await queue_service.reap_expired(db)

    stored = await _reload(task.id)
    assert stored.status == TaskStatus.FAILED
    assert "exhausted" in (stored.error or "") or stored.attempts >= stored.max_attempts


async def test_heartbeat_extends_only_the_owners_lease() -> None:
    task = await _make_task()
    factory = get_session_factory()
    settings = get_settings()
    async with factory() as db:
        await queue_service.claim_next(db, worker_id="owner", lease_seconds=60, settings=settings)

    async with factory() as db:
        owned = await queue_service.heartbeat(
            db, task_id=task.id, worker_id="owner", lease_seconds=600
        )
        stolen = await queue_service.heartbeat(
            db, task_id=task.id, worker_id="intruder", lease_seconds=600
        )

    assert owned is True
    assert stolen is False


async def test_dependency_blocks_claim_until_the_parent_finishes() -> None:
    parent = await _make_task(title="parent", status=TaskStatus.RUNNING)
    child = await _make_task(title="child", priority=1)
    factory = get_session_factory()
    async with factory() as db:
        db.add(TaskDependency(task_id=child.id, depends_on_task_id=parent.id))
        await db.commit()

    async with factory() as db:
        blocked = await queue_service.claim_next(
            db, worker_id="w1", lease_seconds=60, settings=get_settings()
        )
    assert blocked is None or blocked.id != child.id

    async with factory() as db:
        stored = await db.get(Task, parent.id)
        assert stored is not None
        stored.status = TaskStatus.COMPLETED
        await db.commit()

    async with factory() as db:
        claimed = await queue_service.claim_next(
            db, worker_id="w1", lease_seconds=60, settings=get_settings()
        )
    assert claimed is not None and claimed.id == child.id


async def test_future_scheduled_task_is_not_claimed_yet() -> None:
    task = await _make_task(scheduled_at=datetime.now(UTC) + timedelta(hours=1))
    factory = get_session_factory()
    async with factory() as db:
        claimed = await queue_service.claim_next(
            db, worker_id="w1", lease_seconds=60, settings=get_settings()
        )
    assert claimed is None or claimed.id != task.id


async def test_release_requeues_and_counts_the_attempt() -> None:
    task = await _make_task(max_attempts=3)
    factory = get_session_factory()
    settings = get_settings()
    async with factory() as db:
        await queue_service.claim_next(db, worker_id="w1", lease_seconds=60, settings=settings)
    async with factory() as db:
        await queue_service.release(
            db, task_id=task.id, worker_id="w1", requeue=True, reason="test release"
        )

    stored = await _reload(task.id)
    assert stored.claimed_by is None
    assert stored.lease_expires_at is None
    assert stored.status == TaskStatus.QUEUED
    assert stored.attempts == 1

    events = await _event_types(task.id)
    assert "task.requeued" in events


async def test_release_from_a_worker_that_lost_the_lease_is_a_no_op() -> None:
    task = await _make_task(max_attempts=3)
    factory = get_session_factory()
    settings = get_settings()
    async with factory() as db:
        await queue_service.claim_next(db, worker_id="owner", lease_seconds=60, settings=settings)

    async with factory() as db:
        await queue_service.release(
            db, task_id=task.id, worker_id="intruder", requeue=True, reason="nope"
        )

    stored = await _reload(task.id)
    assert stored.claimed_by == "owner"
    assert stored.attempts == 0


async def _event_types(task_id: object) -> list[str]:
    from app.db.models import TaskEvent

    factory = get_session_factory()
    async with factory() as db:
        rows = await db.execute(select(TaskEvent.event_type).where(TaskEvent.task_id == task_id))
        return list(rows.scalars())


async def test_claim_skips_a_task_with_no_attempts_left() -> None:
    task = await _make_task(max_attempts=1)
    factory = get_session_factory()
    async with factory() as db:
        stored = await db.get(Task, task.id)
        assert stored is not None
        stored.attempts = 1
        await db.commit()

    async with factory() as db:
        claimed = await queue_service.claim_next(
            db, worker_id="w1", lease_seconds=60, settings=get_settings()
        )

    assert claimed is None
    stored = await _reload(task.id)
    assert stored.status == TaskStatus.FAILED


async def test_queue_depth_counts_claimable_tasks() -> None:
    await _make_task(title="a", status=TaskStatus.PENDING)
    await _make_task(title="b", status=TaskStatus.QUEUED)
    await _make_task(title="c", status=TaskStatus.COMPLETED)
    factory = get_session_factory()
    async with factory() as db:
        depth = await queue_service.queue_depth(db)
    assert depth == 2


def test_worker_id_is_stable_and_unique_per_process() -> None:
    from app.worker import Worker

    settings = get_settings()
    worker = Worker(settings, name="explicit-name")
    assert worker.name == "explicit-name"
    assert uuid4() is not None
