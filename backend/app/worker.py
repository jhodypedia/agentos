"""Background worker: drains the task queue outside the API process.

Why a separate process: a task runs a model loop for minutes. Running it inside
the request kept the HTTP connection open, made a deploy kill in-flight work, and
meant the API's CPU budget was shared with model reasoning. The API now creates a
task and returns; the worker owns execution.

The worker is deliberately simple — poll, claim one task, run it, repeat. It
scales by running more processes, which is safe because claiming is atomic. A
lease heartbeat runs alongside each task so a long run is not mistaken for a
crash, and a periodic reaper recovers leases left behind by a worker that died.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.events import RedisEventBus
from app.core.exceptions import AppError
from app.core.logging import configure_logging
from app.core.metrics import BACKGROUND_JOBS, TASK_DURATION, registry
from app.core.metrics_server import MetricsServer
from app.db.enums import TaskStatus
from app.db.models import Task
from app.db.session import dispose_engine, get_session_factory
from app.services import execution as execution_service
from app.services import queue as queue_service
from app.tools.builtin import default_tool_registry

logger = structlog.get_logger("agentos.worker")


class Worker:
    """Polls the queue and executes one task at a time."""

    def __init__(
        self,
        settings: Settings,
        *,
        name: str | None = None,
        poll_interval: float = 2.0,
        lease_seconds: int | None = None,
    ) -> None:
        self._settings = settings
        self._poll_interval = poll_interval
        self._lease_seconds = lease_seconds or max(60, settings.worker_lease_seconds)
        host = socket.gethostname()
        self._name = name or f"{host}:{os.getpid()}"
        self._stop = asyncio.Event()
        self._factory: async_sessionmaker[AsyncSession] | None = None
        self._bus: RedisEventBus | None = None
        self._last_reap = 0.0

    # ------------------------------------------------------------- lifecycle
    def request_stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        self._factory = get_session_factory(self._settings)
        self._bus = RedisEventBus(
            self._settings.redis_url,
            socket_timeout=self._settings.redis_socket_timeout_seconds,
        )
        logger.info(
            "worker_started",
            worker=self._name,
            poll_interval=self._poll_interval,
            lease_seconds=self._lease_seconds,
        )
        try:
            while not self._stop.is_set():
                try:
                    worked = await self.run_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # A failure to even reach the database must not kill the
                    # worker: log it, back off, try again.
                    logger.exception("worker_cycle_failed", worker=self._name)
                    worked = False
                if not worked:
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(self._stop.wait(), timeout=self._poll_interval)
        finally:
            if self._bus is not None:
                await self._bus.close()
            await dispose_engine()
            logger.info("worker_stopped", worker=self._name)

    # ------------------------------------------------------------ work cycle
    async def run_once(self) -> bool:
        """Claim and run a single task. Returns whether anything was done."""
        assert self._factory is not None
        await self._maybe_reap()
        async with self._factory() as db:
            task = await queue_service.claim_next(
                db,
                worker_id=self._name,
                lease_seconds=self._lease_seconds,
                settings=self._settings,
            )
        if task is None:
            return False
        await self._execute(task.id)
        return True

    async def _maybe_reap(self) -> None:
        """Reap expired leases at most once every 30 seconds.

        Reaping on every poll would put a locking scan in the hot path of an idle
        worker; doing it on a timer still bounds how long a crashed task waits.
        """
        loop_time = asyncio.get_running_loop().time()
        if loop_time - self._last_reap < 30:
            return
        self._last_reap = loop_time
        assert self._factory is not None
        async with self._factory() as db:
            reaped = await queue_service.reap_expired(db)
        if reaped:
            logger.info("worker_reaped_expired_leases", worker=self._name, count=reaped)

    async def _execute(self, task_id: UUID) -> None:
        assert self._factory is not None
        heartbeat = asyncio.create_task(self._heartbeat_loop(task_id))
        started = datetime.now(UTC)
        BACKGROUND_JOBS.inc(worker=self._name, kind="task")
        try:
            async with self._factory() as db:
                await execution_service.execute_task(
                    db,
                    task_id=task_id,
                    tools=default_tool_registry(),
                    factory=self._factory,
                    bus=self._bus,
                    cancel_check=self._make_cancel_check(task_id),
                )
            logger.info(
                "worker_task_finished",
                worker=self._name,
                task_id=str(task_id),
                seconds=round((datetime.now(UTC) - started).total_seconds(), 2),
            )
        except Exception as exc:
            logger.exception("worker_task_failed", worker=self._name, task_id=str(task_id))
            await self._fail_task(task_id, exc)
        finally:
            # Recorded for failures too: a task that dies after two minutes is
            # still two minutes of a worker that could not do anything else.
            TASK_DURATION.observe((datetime.now(UTC) - started).total_seconds(), worker=self._name)
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat

    def _make_cancel_check(self, task_id: UUID) -> Callable[[], Awaitable[bool]]:
        """Stop a run when the task is cancelled or the worker is shutting down."""

        async def check() -> bool:
            if self._stop.is_set():
                return True
            assert self._factory is not None
            async with self._factory() as db:
                task = await db.get(Task, task_id)
                if task is None:
                    return True
                return task.status == TaskStatus.CANCELLED

        return check

    async def _heartbeat_loop(self, task_id: UUID) -> None:
        assert self._factory is not None
        interval = max(5.0, self._lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            async with self._factory() as db:
                held = await queue_service.heartbeat(
                    db,
                    task_id=task_id,
                    worker_id=self._name,
                    lease_seconds=self._lease_seconds,
                )
            if not held:
                logger.warning("worker_lost_lease", worker=self._name, task_id=str(task_id))
                return

    async def _fail_task(self, task_id: UUID, exc: Exception) -> None:
        """Record a failed run, distinguishing "broken" from "worth retrying".

        A configuration problem — no agent, no model, an unknown provider — will
        fail identically on every attempt, so retrying it only burns the lease
        and hides the real message. A transport or database error might have been
        transient, so that one goes back in the queue.
        """
        assert self._factory is not None
        try:
            async with self._factory() as db:
                task = await db.get(Task, task_id)
                if task is None or task.status in {
                    TaskStatus.COMPLETED,
                    TaskStatus.FAILED,
                    TaskStatus.CANCELLED,
                }:
                    return
                message = f"Worker '{self._name}' crashed: {type(exc).__name__}: {exc}"
                task.error = message[:2000]
                await queue_service.release(
                    db,
                    task_id=task_id,
                    worker_id=self._name,
                    requeue=not isinstance(exc, AppError),
                    reason=message,
                )
        except Exception:  # pragma: no cover - database already broken
            logger.exception("worker_fail_task_failed", task_id=str(task_id))

    # ------------------------------------------------------------ inspection
    @property
    def name(self) -> str:
        return self._name


async def run_worker(settings: Settings | None = None) -> None:
    """Entry point used by ``agentos worker`` and the container command."""
    cfg = settings or get_settings()
    configure_logging(cfg)
    settings_used: Settings = cfg
    worker = Worker(
        settings_used,
        poll_interval=settings_used.worker_poll_interval_seconds,
        lease_seconds=settings_used.worker_lease_seconds,
    )
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, worker.request_stop)
    server = MetricsServer(registry, port=settings_used.worker_metrics_port)
    await server.start()
    try:
        await worker.run()
    finally:
        await server.stop()
