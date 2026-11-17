"""HTTP middleware: request correlation, access logging, metrics and headers."""

from __future__ import annotations

import time
from uuid import uuid4

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.config import Settings
from app.core.metrics import (
    HTTP_DURATION,
    HTTP_IN_FLIGHT,
    HTTP_REQUESTS,
)
from app.core.ratelimit import LimiterStack, build_limiter, client_identity

logger = structlog.get_logger("agentos.http")

# Routes that are polled constantly by orchestrators and scrapers. Rate limiting
# them would break readiness probes and metrics collection during an incident,
# which is exactly when they matter.
_EXEMPT_PREFIXES = ("/health", "/metrics", "/docs", "/redoc", "/openapi.json")


def _route_label(request: Request) -> str:
    """Metric label for the matched route, avoiding one series per path.

    ``request.scope["route"].path`` is available only after routing, which
    happens inside the app. Using the raw path for unmatched requests would let
    a scanner create unbounded label cardinality, so everything unmatched is
    bucketed as ``<unmatched>``.
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if isinstance(path, str):
        return path
    return "<unmatched>"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Attach a request id, log the response and add security headers."""

    def __init__(self, app: object, settings: Settings) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._settings = settings

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        header = self._settings.request_id_header
        request_id = request.headers.get(header) or uuid4().hex
        request.state.request_id = request_id
        # W3C trace id, either propagated from an upstream caller or minted here.
        traceparent = request.headers.get("traceparent")
        trace_id = request_id
        if traceparent:
            parts = traceparent.split("-")
            if len(parts) == 4 and len(parts[1]) == 32:
                trace_id = parts[1]
        request.state.trace_id = trace_id
        structlog.contextvars.bind_contextvars(request_id=request_id, trace_id=trace_id)

        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
        finally:
            duration_s = time.perf_counter() - started
            duration_ms = round(duration_s * 1000, 2)
            route = _route_label(request)
            if not request.url.path.startswith(_EXEMPT_PREFIXES):
                HTTP_REQUESTS.inc(method=request.method, route=route, status=str(status_code))
                HTTP_DURATION.observe(duration_s, method=request.method, route=route)
            logger.info(
                "request",
                method=request.method,
                path=request.url.path,
                route=route,
                status_code=status_code,
                duration_ms=duration_ms,
                client_ip=request.client.host if request.client else None,
            )
            if duration_ms >= self._settings.slow_request_ms:
                logger.warning(
                    "slow_request",
                    method=request.method,
                    path=request.url.path,
                    route=route,
                    status_code=status_code,
                    duration_ms=duration_ms,
                    threshold_ms=self._settings.slow_request_ms,
                )
            structlog.contextvars.clear_contextvars()

        response.headers[header] = request_id
        response.headers["X-Trace-ID"] = trace_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        if self._settings.is_production:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Per-client fixed-window rate limiting with standard headers.

    The limiter is read from ``app.state`` on each request rather than captured
    in ``__init__``: middleware is registered before the lifespan runs, and the
    Redis-backed limiter only exists once the event bus has a client. Until
    then, and in tests that skip the lifespan, a per-process limiter is used.
    """

    def __init__(self, app: object, settings: Settings) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._settings = settings
        self._fallback = build_limiter(None, name="http")
        self._fallback_auth = build_limiter(None, name="auth")

    def _limiters(self, request: Request) -> tuple[LimiterStack, LimiterStack]:
        state = request.app.state
        return (
            getattr(state, "rate_limiter", None) or self._fallback,
            getattr(state, "auth_rate_limiter", None) or self._fallback_auth,
        )

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not self._settings.rate_limit_enabled or request.method == "OPTIONS":
            return await call_next(request)
        path = request.url.path
        if path.startswith(_EXEMPT_PREFIXES):
            return await call_next(request)

        limiter, auth_limiter = self._limiters(request)
        login_attempt = path.endswith("/auth/login") or path.endswith("/auth/password/reset")
        active, limit, window = (
            (
                auth_limiter,
                self._settings.auth_rate_limit_requests,
                self._settings.auth_rate_limit_window_seconds,
            )
            if login_attempt
            else (
                limiter,
                self._settings.rate_limit_requests,
                self._settings.rate_limit_window_seconds,
            )
        )
        identity = client_identity(
            dict(request.headers),
            request.client.host if request.client else None,
            trust_forwarded_for=self._settings.trust_forwarded_for,
        )
        decision = await active.check(identity, limit=limit, window_seconds=window)
        if not decision.allowed:
            logger.warning("rate_limited", path=request.url.path, method=request.method)
            limited: Response = JSONResponse(
                status_code=429,
                content={
                    "error": {
                        "code": "rate_limited",
                        "message": "Too many requests; slow down and retry",
                        "details": {"limit": decision.limit, "window_seconds": window},
                        "request_id": getattr(request.state, "request_id", None),
                    }
                },
            )
            limited.headers.update(decision.headers())
            return limited

        response = await call_next(request)
        response.headers.update(decision.headers() if self._settings.rate_limit_headers else {})
        return response


class InFlightMiddleware(BaseHTTPMiddleware):
    """Track concurrency for the metrics endpoint."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        HTTP_IN_FLIGHT.inc()
        try:
            request.state.in_flight = HTTP_IN_FLIGHT.value()
            return await call_next(request)
        finally:
            HTTP_IN_FLIGHT.dec()
