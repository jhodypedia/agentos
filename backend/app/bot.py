"""Telegram notification runner.

The bot is pull-based on our side: Telegram pushes updates to the webhook, but
outbound notifications (task finished, approval waiting) have to be initiated by
us. That is this loop — it re-reads committed state and sends whatever has not
been delivered yet, marking each message on the row so a restart does not spam
the operator.

Deliberately a separate process from both the API and the worker: a Telegram
outage or a slow Bot API call must never stall task execution, and the loop does
nothing at all when no bot token is configured.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal

import structlog

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db.session import dispose_engine, get_session_factory
from app.telegram.notifications import deliver

logger = structlog.get_logger("agentos.bot")

TOKEN_WAIT_SECONDS = 300


def _install_stop_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)


async def run_bot(settings: Settings | None = None) -> None:
    cfg = settings or get_settings()
    configure_logging(cfg)
    stop = asyncio.Event()
    _install_stop_handlers(stop)

    if not cfg.telegram_bot_token:
        logger.warning("bot_disabled", reason="TELEGRAM_BOT_TOKEN is not configured")
        if cfg.environment == "testing":
            return
        # Returning here would let a restart policy bring the container straight
        # back, which is a crash loop in everything but exit code. Wait instead,
        # and start working the moment a token appears — no redeploy needed.
        while not stop.is_set():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=TOKEN_WAIT_SECONDS)
            fresh = get_settings()
            if fresh.telegram_bot_token:
                logger.info("bot_token_appeared")
                cfg = fresh
                break
        if stop.is_set():
            return

    factory = get_session_factory(cfg)
    interval = cfg.telegram_notify_interval_seconds
    logger.info("bot_started", interval_seconds=interval)
    try:
        while not stop.is_set():
            try:
                sent = await deliver(factory, cfg)
                if sent:
                    logger.info("bot_notifications_sent", count=sent)
            except asyncio.CancelledError:
                raise
            except Exception:
                # A Telegram failure is retried on the next tick; the rows stay
                # unmarked, so nothing is lost.
                logger.exception("bot_cycle_failed")
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval)
    finally:
        await dispose_engine()
        logger.info("bot_stopped")
