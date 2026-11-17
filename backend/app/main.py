"""FastAPI application factory and ASGI entrypoint."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.health import router as health_router
from app.api.metrics import router as metrics_router
from app.api.v1.router import api_router
from app.core.config import Settings, get_settings
from app.core.events import RedisEventBus
from app.core.exceptions import AppError
from app.core.logging import configure_logging, get_logger
from app.core.middleware import (
    InFlightMiddleware,
    RateLimitMiddleware,
    RequestContextMiddleware,
)
from app.core.ratelimit import build_limiter
from app.db.session import dispose_engine, get_engine, get_session_factory
from app.schemas.common import ErrorBody, ErrorResponse

logger = get_logger("agentos.app")


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def _error(
    status_code: int,
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
    request_id: str | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorBody(
            code=code,
            message=message,
            details=details or {},
            request_id=request_id,
        )
    )
    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(mode="json"),
        headers=headers,
    )


async def _sync_builtin_tools(settings: Settings) -> None:
    """Keep ``tool_registry`` in step with the code-defined built-in tools."""
    from app.services.agents import sync_tool_registry
    from app.tools.builtin import BUILTIN_TOOLS

    try:
        factory = get_session_factory(settings)
        async with factory() as session:
            counts = await sync_tool_registry(session, [tool() for tool in BUILTIN_TOOLS])
            await session.commit()
    except Exception:
        logger.exception("tool_registry_sync_failed")
        return
    logger.info(
        "tool_registry_synced",
        created=counts["created"],
        updated=counts["updated"],
    )


async def _sync_builtin_skills(settings: Settings) -> None:
    """Install the shipped skills into the managed root and register them.

    Non-fatal: a broken skills directory must not stop the API from starting.
    """
    from app.services.skills import sync_builtin_skills

    try:
        factory = get_session_factory(settings)
        async with factory() as session:
            report = await sync_builtin_skills(session)
            await session.commit()
    except Exception:
        logger.exception("builtin_skills_sync_failed")
        return
    logger.info(
        "builtin_skills_synced",
        installed=len(report.installed),
        failed=len(report.failed),
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    configure_logging(settings)
    logger.info(
        "application_starting",
        name=settings.app_name,
        version=settings.app_version,
        environment=settings.environment,
    )

    if settings.sandbox_enabled:
        settings.workspace_root.mkdir(parents=True, exist_ok=True)

    get_engine(settings)
    app.state.session_factory = get_session_factory(settings)
    app.state.event_bus = RedisEventBus(
        settings.redis_url, socket_timeout=settings.redis_socket_timeout_seconds
    )
    # The Redis client is created eagerly but connects lazily; a limiter built on
    # an unreachable Redis fails per request and the stack falls back in-process.
    app.state.rate_limiter = build_limiter(app.state.event_bus.client, name="http")
    app.state.auth_rate_limiter = build_limiter(app.state.event_bus.client, name="auth")
    await _sync_builtin_tools(settings)
    await _sync_builtin_skills(settings)

    try:
        yield
    finally:
        await app.state.event_bus.close()
        await dispose_engine()
        logger.info("application_stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or get_settings()
    configure_logging(cfg)

    app = FastAPI(
        title=cfg.app_name,
        version=cfg.app_version,
        description="Self-hosted autonomous multi-agent AI platform",
        lifespan=lifespan,
        docs_url=None if cfg.is_production else "/docs",
        redoc_url=None if cfg.is_production else "/redoc",
        openapi_url=None if cfg.is_production else "/openapi.json",
        swagger_ui_parameters={"persistAuthorization": True},
    )
    app.state.settings = cfg

    # Middleware runs in reverse registration order (last added is outermost).
    # The order is deliberate:
    #   RequestContext (outermost) -> CORS -> RateLimit -> InFlight (innermost)
    # CORS has to sit outside the limiter or a 429 comes back without
    # Access-Control-Allow-Origin and the browser reports a CORS failure instead
    # of showing the rate-limit message. RequestContext stays outermost so even
    # a rejection gets a request id, a trace id and a log line.
    app.add_middleware(InFlightMiddleware)
    app.add_middleware(RateLimitMiddleware, settings=cfg)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-API-Key", cfg.request_id_header],
        expose_headers=[
            cfg.request_id_header,
            "X-Trace-ID",
            "Retry-After",
            "X-RateLimit-Limit",
            "X-RateLimit-Remaining",
            "X-RateLimit-Reset",
        ],
        max_age=600,
    )
    app.add_middleware(RequestContextMiddleware, settings=cfg)

    # ------------------------------------------------------------------ errors
    @app.exception_handler(AppError)
    async def _app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        if exc.status_code >= 500:
            logger.error("application_error", code=exc.code, message=exc.message, exc_info=exc)
        return _error(
            exc.status_code,
            exc.code,
            exc.message,
            details=exc.details,
            request_id=_request_id(request),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return _error(
            422,
            "validation_error",
            "The request payload is invalid",
            details={"errors": exc.errors()},
            request_id=_request_id(request),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        message = exc.detail if isinstance(exc.detail, str) else "HTTP error"
        return _error(
            exc.status_code,
            "http_error",
            message,
            request_id=_request_id(request),
            headers=dict(exc.headers) if exc.headers else None,
        )

    @app.exception_handler(Exception)
    async def _unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.error("unhandled_exception", exc_info=exc)
        return _error(
            500,
            "internal_error",
            "An unexpected error occurred",
            request_id=_request_id(request),
        )

    # ------------------------------------------------------------------ routes
    app.include_router(health_router)
    app.include_router(metrics_router)
    app.include_router(api_router, prefix=cfg.api_prefix)

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, str]:
        return {
            "name": cfg.app_name,
            "version": cfg.app_version,
            "api": cfg.api_prefix,
            "docs": "/docs" if not cfg.is_production else "disabled",
        }

    return app


app = create_app()
