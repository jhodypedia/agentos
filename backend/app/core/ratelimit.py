"""Request rate limiting.

Two implementations behind one interface:

* :class:`RedisRateLimiter` — a fixed window counter shared by every API
  process. This is the one used when Redis is reachable.
* :class:`MemoryRateLimiter` — a per-process fallback used only when Redis
  fails, so a Redis outage degrades the limit rather than disabling it. It is
  honestly per-process: with N workers the effective limit is N times the
  configured one.

Fixed windows are used rather than a token bucket because the counter is a
single ``INCR`` plus a conditional ``EXPIRE``: one round trip, no Lua, and the
worst-case burst is a documented 2x across a window boundary.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Protocol

import structlog

from app.core.metrics import RATE_LIMITED

logger = structlog.get_logger("agentos.ratelimit")

_KEY_PREFIX = "agentos:rl"


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    limit: int
    remaining: int
    retry_after: int

    def headers(self) -> dict[str, str]:
        headers = {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(max(0, self.remaining)),
        }
        if not self.allowed:
            headers["Retry-After"] = str(max(1, self.retry_after))
        return headers


class RateLimiter(Protocol):
    scope: str

    async def check(self, identity: str, *, limit: int, window_seconds: int) -> RateLimitDecision:
        """Count one request for ``identity`` and report whether it may proceed."""


def _window(now: float, window_seconds: int) -> tuple[int, int]:
    window = max(1, int(window_seconds))
    start = int(now // window) * window
    retry_after = max(1, int(start + window - now))
    return start, retry_after


class RedisRateLimiter:
    """Fixed-window counter in Redis, shared across processes."""

    def __init__(self, client: Any, *, name: str) -> None:
        self._client = client
        self.scope = name

    async def check(self, identity: str, *, limit: int, window_seconds: int) -> RateLimitDecision:
        start, retry_after = _window(time.time(), window_seconds)
        key = f"{_KEY_PREFIX}:{self.scope}:{identity}:{start}"
        pipe = self._client.pipeline()
        pipe.incr(key)
        pipe.expire(key, max(1, int(window_seconds)) + 1)
        count = int((await pipe.execute())[0])
        return RateLimitDecision(
            allowed=count <= limit,
            limit=limit,
            remaining=limit - count,
            retry_after=retry_after,
        )


class MemoryRateLimiter:
    """Per-process fixed-window counter, used when Redis is unavailable."""

    def __init__(self, *, name: str) -> None:
        self.scope = name
        self._counters: dict[str, tuple[int, int]] = {}
        self._lock = asyncio.Lock()

    async def check(self, identity: str, *, limit: int, window_seconds: int) -> RateLimitDecision:
        start, retry_after = _window(time.time(), window_seconds)
        async with self._lock:
            key = f"{identity}:{start}"
            previous_start, count = self._counters.get(key, (start, 0))
            if previous_start != start:
                count = 0
            count += 1
            self._counters[key] = (start, count)
            if len(self._counters) > 10_000:
                self._counters = {
                    item: value for item, value in self._counters.items() if value[0] == start
                }
        return RateLimitDecision(
            allowed=count <= limit,
            limit=limit,
            remaining=limit - count,
            retry_after=retry_after,
        )


class LimiterStack:
    """Primary limiter plus fallback, with the fallback's use made visible."""

    def __init__(self, primary: RateLimiter, fallback: RateLimiter) -> None:
        self._primary = primary
        self._fallback = fallback
        self.scope = primary.scope

    async def check(self, identity: str, *, limit: int, window_seconds: int) -> RateLimitDecision:
        try:
            decision = await self._primary.check(
                identity, limit=limit, window_seconds=window_seconds
            )
        except Exception as exc:
            logger.warning("rate_limiter_degraded", scope=self.scope, error=type(exc).__name__)
            decision = await self._fallback.check(
                identity, limit=limit, window_seconds=window_seconds
            )
        if not decision.allowed:
            RATE_LIMITED.inc(scope=self.scope)
        return decision


def build_limiter(redis_client: Any | None, *, name: str) -> LimiterStack:
    """Build the limiter pair used by the HTTP middleware.

    With no Redis client the primary is a memory limiter as well, which is the
    correct behaviour during startup and in tests: the limit still applies, it
    is just not shared between processes.
    """
    primary: RateLimiter = (
        RedisRateLimiter(redis_client, name=name)
        if redis_client is not None
        else MemoryRateLimiter(name=name)
    )
    return LimiterStack(primary, MemoryRateLimiter(name=name))


def client_identity(
    request_headers: dict[str, str], client_host: str | None, *, trust_forwarded_for: bool = False
) -> str:
    """Identify the caller for rate limiting.

    ``X-Forwarded-For`` is only honoured when the deployment says a trusted
    proxy sets it (``TRUST_FORWARDED_FOR=true``); otherwise any client could
    pick its own bucket by sending the header. The first hop is used, which is
    what a single reverse proxy in front of the API writes.
    """
    if trust_forwarded_for:
        forwarded = request_headers.get("x-forwarded-for")
        if forwarded:
            first = forwarded.split(",")[0].strip()
            if first:
                return first
    return client_host or "unknown"
