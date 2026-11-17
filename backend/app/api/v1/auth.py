"""Authentication endpoints: login, current user, API key management."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Request, status
from sqlalchemy import select

from app.api.deps import DbSession, PrincipalDep
from app.core.config import get_settings
from app.core.exceptions import ConflictError, NotFoundError, UnauthorizedError
from app.core.security import create_access_token, generate_api_key
from app.db.models import ApiKey
from app.schemas.user import (
    ApiKeyCreatedResponse,
    ApiKeyCreateRequest,
    ApiKeyPublic,
    LoginRequest,
    TokenResponse,
    UserPublic,
)
from app.services.audit import record_audit
from app.services.users import authenticate

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse, summary="Exchange credentials for a JWT")
async def login(payload: LoginRequest, request: Request, db: DbSession) -> TokenResponse:
    user = await authenticate(db, payload.email, payload.password)
    if user is None:
        await record_audit(
            db,
            action="auth.login_failed",
            resource_type="user",
            success=False,
            request=request,
            meta={"email": payload.email.lower()},
        )
        await db.commit()
        raise UnauthorizedError("Invalid email or password")

    now = datetime.now(UTC)
    user.last_login_at = now
    token, _claims = create_access_token(user.id, user.role.value, scopes=["*"])
    await record_audit(
        db,
        action="auth.login",
        resource_type="user",
        resource_id=user.id,
        actor_user_id=user.id,
        request=request,
    )
    await db.commit()
    return TokenResponse(
        access_token=token,
        expires_in=get_settings().access_token_ttl_minutes * 60,
    )


@router.get("/me", response_model=UserPublic, summary="Return the authenticated user")
async def me(principal: PrincipalDep) -> UserPublic:
    return UserPublic.model_validate(principal.user)


@router.post(
    "/api-keys",
    response_model=ApiKeyCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an API key (returned once)",
)
async def create_api_key(
    payload: ApiKeyCreateRequest,
    request: Request,
    principal: PrincipalDep,
    db: DbSession,
) -> ApiKeyCreatedResponse:
    if principal.auth_type != "jwt":
        raise UnauthorizedError("API keys can only be created with a user session")

    generated = generate_api_key()
    expires_at = (
        datetime.now(UTC) + timedelta(days=payload.expires_in_days)
        if payload.expires_in_days
        else None
    )
    key = ApiKey(
        user_id=principal.user.id,
        name=payload.name,
        prefix=generated.prefix,
        key_hash=generated.key_hash,
        scopes=payload.scopes,
        expires_at=expires_at,
    )
    db.add(key)
    await db.flush()
    await record_audit(
        db,
        action="auth.api_key_created",
        resource_type="api_key",
        resource_id=key.id,
        actor_user_id=principal.user.id,
        request=request,
        meta={"name": key.name, "scopes": key.scopes},
    )
    await db.commit()
    await db.refresh(key)
    return ApiKeyCreatedResponse(
        id=key.id,
        name=key.name,
        prefix=key.prefix,
        key=generated.plaintext,
        scopes=key.scopes,
        expires_at=key.expires_at,
        created_at=key.created_at,
    )


@router.get("/api-keys", response_model=list[ApiKeyPublic], summary="List your API keys")
async def list_api_keys(principal: PrincipalDep, db: DbSession) -> list[ApiKeyPublic]:
    result = await db.execute(
        select(ApiKey).where(ApiKey.user_id == principal.user.id).order_by(ApiKey.created_at.desc())
    )
    return [ApiKeyPublic.model_validate(key) for key in result.scalars()]


@router.delete(
    "/api-keys/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke an API key",
)
async def revoke_api_key(
    key_id: UUID,
    request: Request,
    principal: PrincipalDep,
    db: DbSession,
) -> None:
    key = await db.get(ApiKey, key_id)
    if key is None or key.user_id != principal.user.id:
        raise NotFoundError("API key not found")
    if key.revoked_at is not None:
        raise ConflictError("The API key is already revoked")
    key.revoked_at = datetime.now(UTC)
    await record_audit(
        db,
        action="auth.api_key_revoked",
        resource_type="api_key",
        resource_id=key.id,
        actor_user_id=principal.user.id,
        request=request,
    )
    await db.commit()
