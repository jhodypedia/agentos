"""Phase 10: metrics registry, scrape endpoint, rate limiting and tracing."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.metrics import Counter, Histogram, Registry
from app.core.ratelimit import (
    MemoryRateLimiter,
    RateLimitDecision,
    build_limiter,
    client_identity,
)

# --------------------------------------------------------------------- registry


def test_counter_accumulates_and_renders() -> None:
    registry = Registry()
    counter = registry.counter("test_counter_total", "A counter")
    counter.inc()
    counter.inc(2, method="GET")
    assert counter.value() == 1
    assert counter.value(method="GET") == 2
    rendered = registry.render()
    assert "# TYPE test_counter_total counter" in rendered
    assert 'test_counter_total{method="GET"} 2.0' in rendered


def test_counter_rejects_negative_increments() -> None:
    registry = Registry()
    counter = registry.counter("test_negative_total", "A counter")
    with pytest.raises(ValueError):
        counter.inc(-1)


def test_gauge_can_go_down() -> None:
    registry = Registry()
    gauge = registry.gauge("test_gauge", "A gauge")
    gauge.set(5)
    gauge.dec(2)
    assert gauge.value() == 3
    gauge.inc()
    assert gauge.value() == 4


def test_histogram_buckets_are_cumulative() -> None:
    registry = Registry()
    histogram: Histogram = registry.histogram(
        "test_histogram_seconds", "A histogram", buckets=(0.1, 1.0)
    )
    histogram.observe(0.05)
    histogram.observe(0.5)
    histogram.observe(2.0)
    rendered = registry.render()
    assert 'test_histogram_seconds_bucket{le="0.1"} 1.0' in rendered
    assert 'test_histogram_seconds_bucket{le="1.0"} 2.0' in rendered
    assert 'test_histogram_seconds_bucket{le="+Inf"} 3.0' in rendered
    assert "test_histogram_seconds_count 3.0" in rendered


def test_registry_refuses_reusing_a_name_with_another_type() -> None:
    registry = Registry()
    registry.counter("duplicate_total", "A counter")
    with pytest.raises(TypeError):
        registry.gauge("duplicate_total", "A gauge")


async def test_metric_increments_are_thread_safe() -> None:
    registry = Registry()
    counter: Counter = registry.counter("test_concurrent_total", "A counter")

    def bump() -> None:
        for _ in range(100):
            counter.inc()

    import asyncio

    await asyncio.gather(*(asyncio.to_thread(bump) for _ in range(10)))
    assert counter.value() == 1000


# --------------------------------------------------------------------- limiter


async def test_memory_limiter_allows_up_to_the_limit() -> None:
    limiter = MemoryRateLimiter(name="test")
    decisions = [await limiter.check("1.2.3.4", limit=3, window_seconds=60) for _ in range(4)]
    assert [decision.allowed for decision in decisions] == [True, True, True, False]
    assert decisions[2].remaining == 0
    assert decisions[3].retry_after >= 1


async def test_memory_limiter_isolates_identities() -> None:
    limiter = MemoryRateLimiter(name="test")
    await limiter.check("a", limit=1, window_seconds=60)
    assert (await limiter.check("a", limit=1, window_seconds=60)).allowed is False
    assert (await limiter.check("b", limit=1, window_seconds=60)).allowed is True


def test_decision_headers_include_retry_after_only_when_blocked() -> None:
    allowed = RateLimitDecision(allowed=True, limit=10, remaining=4, retry_after=12)
    assert "Retry-After" not in allowed.headers()
    assert allowed.headers()["X-RateLimit-Remaining"] == "4"

    blocked = RateLimitDecision(allowed=False, limit=10, remaining=-2, retry_after=12)
    assert blocked.headers()["Retry-After"] == "12"
    assert blocked.headers()["X-RateLimit-Remaining"] == "0"


def test_client_identity_ignores_forwarded_for_by_default() -> None:
    headers = {"x-forwarded-for": "9.9.9.9, 10.0.0.1"}
    assert client_identity(headers, "10.0.0.5") == "10.0.0.5"
    assert client_identity(headers, "10.0.0.5", trust_forwarded_for=True) == "9.9.9.9"
    assert client_identity({}, None) == "unknown"


def test_build_limiter_without_redis_still_limits() -> None:
    stack = build_limiter(None, name="test")
    assert stack.scope == "test"


async def test_limiter_stack_falls_back_when_primary_fails() -> None:
    class Broken:
        scope = "broken"

        async def check(
            self, identity: str, *, limit: int, window_seconds: int
        ) -> RateLimitDecision:
            raise ConnectionError("redis is down")

    from app.core.ratelimit import LimiterStack

    stack = LimiterStack(Broken(), MemoryRateLimiter(name="fallback"))  # type: ignore[arg-type]
    decision = await stack.check("client", limit=2, window_seconds=60)
    assert decision.allowed is True
    assert decision.limit == 2


# ----------------------------------------------------------------- middleware


@pytest.mark.integration
async def test_health_is_exempt_from_rate_limiting(app_client: AsyncClient) -> None:
    for _ in range(5):
        response = await app_client.get("/health/live")
        assert response.status_code == 200


@pytest.mark.integration
async def test_rate_limit_rejects_with_envelope_and_headers(app_client: AsyncClient) -> None:
    from app.core.config import get_settings
    from app.main import create_app

    settings = get_settings()
    original_requests = settings.rate_limit_requests
    original_window = settings.rate_limit_window_seconds
    settings.rate_limit_requests = 2
    settings.rate_limit_window_seconds = 60
    try:
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            statuses = [(await client.get("/api/v1/meta")).status_code for _ in range(4)]
            blocked = await client.get("/api/v1/meta")
    finally:
        settings.rate_limit_requests = original_requests
        settings.rate_limit_window_seconds = original_window

    assert statuses[:2] == [200, 200]
    assert statuses[2] == 429
    assert statuses[3] == 429
    assert blocked.status_code == 429
    assert blocked.headers["X-RateLimit-Limit"] == "2"
    assert "Retry-After" in blocked.headers
    error = blocked.json()["error"]
    assert error["code"] == "rate_limited"
    assert error["request_id"]


@pytest.mark.integration
async def test_rate_limit_headers_are_reported_on_success(app_client: AsyncClient) -> None:
    response = await app_client.get("/api/v1/meta")
    assert response.status_code == 200
    assert response.headers["X-RateLimit-Limit"]
    assert response.headers["X-RateLimit-Remaining"].isdigit()


@pytest.mark.integration
async def test_traceparent_is_propagated(app_client: AsyncClient) -> None:
    trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"
    response = await app_client.get(
        "/health/live", headers={"traceparent": f"00-{trace_id}-00f067aa0ba902b7-01"}
    )
    assert response.status_code == 200
    assert response.headers["X-Trace-ID"] == trace_id


@pytest.mark.integration
async def test_invalid_traceparent_falls_back_to_request_id(app_client: AsyncClient) -> None:
    response = await app_client.get("/health/live", headers={"traceparent": "not-a-traceparent"})
    assert response.headers["X-Trace-ID"] == response.headers["X-Request-ID"]


# ---------------------------------------------------------------- metrics route


@pytest.mark.integration
async def test_metrics_endpoint_exposes_prometheus_text(app_client: AsyncClient) -> None:
    response = await app_client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    body = response.text
    assert "# HELP agentos_http_requests_total" in body
    assert "agentos_tasks{" in body


@pytest.mark.integration
async def test_metrics_endpoint_requires_token_when_configured(app_client: AsyncClient) -> None:
    from pydantic import SecretStr

    from app.core.config import get_settings

    settings = get_settings()
    original = settings.metrics_token
    settings.metrics_token = SecretStr("scrape-token")
    try:
        denied = await app_client.get("/metrics")
        assert denied.status_code == 401
        assert denied.headers["www-authenticate"].startswith("Bearer")
        allowed = await app_client.get("/metrics", headers={"Authorization": "Bearer scrape-token"})
        assert allowed.status_code == 200
    finally:
        settings.metrics_token = original


@pytest.mark.integration
async def test_rate_limited_response_keeps_cors_headers(
    app_client: AsyncClient, db_session
) -> None:
    """A 429 must still be readable by the browser.

    CORS was registered before the limiter, which made it innermost, so the
    limiter's short-circuit response never passed through it: the browser saw a
    CORS error instead of the rate-limit message. The dashboard calls the API
    cross-origin, so this made throttling look like a broken dashboard.
    """
    from app.core.config import get_settings

    settings = get_settings()
    limit = settings.auth_rate_limit_requests
    origin = "http://localhost:3000"

    statuses = []
    for _ in range(limit + 1):
        response = await app_client.post(
            "/api/v1/auth/login",
            json={"email": "nobody@example.com", "password": "wrong-password-here"},
            headers={"Origin": origin},
        )
        statuses.append(response.status_code)
        last = response

    assert statuses[-1] == 429
    assert last.json()["error"]["code"] == "rate_limited"
    assert last.headers["access-control-allow-origin"] == origin
    assert "retry-after" in last.headers
    assert "x-request-id" in last.headers
    assert "x-trace-id" in last.headers
