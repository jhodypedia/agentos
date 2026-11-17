"""Model provider administration."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError
from app.core.security import encrypt_secret
from app.db.enums import ProviderType
from app.db.models import ModelProvider, ModelUsage
from app.providers.registry import provider_from_record

logger = structlog.get_logger("agentos.services.providers")


async def list_providers(db: AsyncSession) -> list[ModelProvider]:
    result = await db.execute(select(ModelProvider).order_by(ModelProvider.slug))
    return list(result.scalars())


async def get_provider(db: AsyncSession, provider_id: UUID) -> ModelProvider:
    provider = await db.get(ModelProvider, provider_id)
    if provider is None:
        raise NotFoundError("Model provider not found")
    return provider


async def get_provider_by_slug(db: AsyncSession, slug: str) -> ModelProvider | None:
    result = await db.execute(select(ModelProvider).where(ModelProvider.slug == slug))
    return result.scalar_one_or_none()


async def create_provider(
    db: AsyncSession,
    *,
    slug: str,
    name: str,
    provider_type: ProviderType,
    base_url: str | None = None,
    api_key: str | None = None,
    default_model: str | None = None,
    enabled: bool = False,
    config: dict[str, Any] | None = None,
    created_by: UUID | None = None,
) -> ModelProvider:
    if await get_provider_by_slug(db, slug) is not None:
        raise ConflictError(f"A provider with slug '{slug}' already exists")

    provider = ModelProvider(
        slug=slug.strip().lower(),
        name=name.strip(),
        provider_type=provider_type,
        base_url=base_url.strip() if base_url else None,
        default_model=default_model,
        enabled=enabled,
        config=config or {},
        created_by=created_by,
    )
    if api_key:
        provider.api_key_encrypted = encrypt_secret(api_key)
        provider.api_key_hint = _hint(api_key)
    db.add(provider)
    await db.flush()
    return provider


async def update_provider(
    db: AsyncSession,
    provider: ModelProvider,
    *,
    name: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    clear_api_key: bool = False,
    default_model: str | None = None,
    enabled: bool | None = None,
    config: dict[str, Any] | None = None,
) -> ModelProvider:
    if name is not None:
        provider.name = name.strip()
    if base_url is not None:
        provider.base_url = base_url.strip() or None
    if api_key:
        provider.api_key_encrypted = encrypt_secret(api_key)
        provider.api_key_hint = _hint(api_key)
    elif clear_api_key:
        provider.api_key_encrypted = None
        provider.api_key_hint = None
    if default_model is not None:
        provider.default_model = default_model or None
    if enabled is not None:
        provider.enabled = enabled
    if config is not None:
        provider.config = {**(provider.config or {}), **config}
    await db.flush()
    return provider


async def delete_provider(db: AsyncSession, provider: ModelProvider) -> None:
    await db.delete(provider)
    await db.flush()


async def check_provider_health(db: AsyncSession, provider: ModelProvider) -> dict[str, Any]:
    """Call the provider's model-listing endpoint and record the outcome."""
    from datetime import UTC, datetime

    try:
        adapter = provider_from_record(provider, timeout_seconds=20.0, max_retries=0)
    except Exception as exc:
        provider.health_status = "misconfigured"
        provider.last_health_check_at = datetime.now(UTC)
        await db.flush()
        return {"ok": False, "detail": str(exc), "models": []}

    try:
        health = await adapter.health()
    finally:
        await adapter.aclose()

    provider.health_status = "ok" if health.ok else "error"
    provider.last_health_check_at = datetime.now(UTC)
    await db.flush()
    return {
        "ok": health.ok,
        "detail": health.detail,
        "models": health.models[:200],
        "latency_ms": health.latency_ms,
    }


async def usage_summary(db: AsyncSession, provider_id: UUID | None = None) -> dict[str, Any]:
    statement = select(
        func.count(ModelUsage.id),
        func.coalesce(func.sum(ModelUsage.total_tokens), 0),
        func.coalesce(func.sum(ModelUsage.cost_usd), 0),
    )
    if provider_id is not None:
        statement = statement.where(ModelUsage.provider_id == provider_id)
    row = (await db.execute(statement)).one()
    return {
        "requests": int(row[0]),
        "total_tokens": int(row[1]),
        "cost_usd": str(row[2]),
    }


def _hint(api_key: str) -> str:
    stripped = api_key.strip()
    if len(stripped) <= 4:
        return "****"
    return f"…{stripped[-4:]}"
