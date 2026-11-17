"""Model provider endpoints."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Request, status

from app.api.deps import AdminPrincipal, DbSession, OperatorPrincipal
from app.schemas.provider import (
    ProviderCreate,
    ProviderHealthResponse,
    ProviderPublic,
    ProviderUpdate,
    ProviderUsageResponse,
)
from app.services import providers as provider_service
from app.services.audit import record_audit

router = APIRouter(prefix="/providers", tags=["providers"])


@router.get("", response_model=list[ProviderPublic], summary="List model providers")
async def list_providers(principal: OperatorPrincipal, db: DbSession) -> list[ProviderPublic]:
    providers = await provider_service.list_providers(db)
    return [ProviderPublic.model_validate(item) for item in providers]


@router.post(
    "",
    response_model=ProviderPublic,
    status_code=status.HTTP_201_CREATED,
    summary="Register a model provider",
)
async def create_provider(
    payload: ProviderCreate,
    principal: AdminPrincipal,
    db: DbSession,
    request: Request,
) -> ProviderPublic:
    provider = await provider_service.create_provider(
        db,
        slug=payload.slug,
        name=payload.name,
        provider_type=payload.provider_type,
        base_url=payload.base_url,
        api_key=payload.api_key,
        default_model=payload.default_model,
        enabled=payload.enabled,
        config=payload.config,
        created_by=principal.user.id,
    )
    await record_audit(
        db,
        action="provider.created",
        resource_type="model_provider",
        resource_id=provider.id,
        actor_user_id=principal.user.id,
        request=request,
        meta={"slug": provider.slug, "type": provider.provider_type.value},
    )
    await db.commit()
    await db.refresh(provider)
    return ProviderPublic.model_validate(provider)


@router.get("/{provider_id}", response_model=ProviderPublic, summary="Get a provider")
async def get_provider(
    provider_id: UUID, principal: OperatorPrincipal, db: DbSession
) -> ProviderPublic:
    provider = await provider_service.get_provider(db, provider_id)
    return ProviderPublic.model_validate(provider)


@router.patch("/{provider_id}", response_model=ProviderPublic, summary="Update a provider")
async def update_provider(
    provider_id: UUID,
    payload: ProviderUpdate,
    principal: AdminPrincipal,
    db: DbSession,
    request: Request,
) -> ProviderPublic:
    provider = await provider_service.get_provider(db, provider_id)
    updated = await provider_service.update_provider(
        db,
        provider,
        name=payload.name,
        base_url=payload.base_url,
        api_key=payload.api_key,
        clear_api_key=payload.clear_api_key,
        default_model=payload.default_model,
        enabled=payload.enabled,
        config=payload.config,
    )
    await record_audit(
        db,
        action="provider.updated",
        resource_type="model_provider",
        resource_id=updated.id,
        actor_user_id=principal.user.id,
        request=request,
        meta={"key_rotated": bool(payload.api_key), "enabled": updated.enabled},
    )
    await db.commit()
    await db.refresh(updated)
    return ProviderPublic.model_validate(updated)


@router.delete(
    "/{provider_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a provider",
)
async def delete_provider(
    provider_id: UUID,
    principal: AdminPrincipal,
    db: DbSession,
    request: Request,
) -> None:
    provider = await provider_service.get_provider(db, provider_id)
    slug = provider.slug
    await provider_service.delete_provider(db, provider)
    await record_audit(
        db,
        action="provider.deleted",
        resource_type="model_provider",
        resource_id=provider_id,
        actor_user_id=principal.user.id,
        request=request,
        meta={"slug": slug},
    )
    await db.commit()


@router.post(
    "/{provider_id}/health",
    response_model=ProviderHealthResponse,
    summary="Check provider connectivity with the stored credentials",
)
async def provider_health(
    provider_id: UUID,
    principal: OperatorPrincipal,
    db: DbSession,
) -> ProviderHealthResponse:
    provider = await provider_service.get_provider(db, provider_id)
    result = await provider_service.check_provider_health(db, provider)
    await db.commit()
    return ProviderHealthResponse(**result)


@router.get(
    "/{provider_id}/usage",
    response_model=ProviderUsageResponse,
    summary="Aggregate token and cost usage for a provider",
)
async def provider_usage(
    provider_id: UUID, principal: OperatorPrincipal, db: DbSession
) -> ProviderUsageResponse:
    await provider_service.get_provider(db, provider_id)
    summary = await provider_service.usage_summary(db, provider_id)
    return ProviderUsageResponse(**summary)
