"""Shared FastAPI dependencies: database sessions, authentication, RBAC."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import Depends, Request
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import PermissionDeniedError, UnauthorizedError
from app.core.security import decode_access_token, hash_api_key
from app.db.enums import UserRole
from app.db.models import ApiKey, User
from app.db.session import get_db

DbSession = Annotated[AsyncSession, Depends(get_db)]

_bearer_scheme = HTTPBearer(auto_error=False, description="JWT access token")
_api_key_scheme = APIKeyHeader(name="X-API-Key", auto_error=False, description="API key")

_ROLE_RANK: dict[UserRole, int] = {
    UserRole.VIEWER: 10,
    UserRole.OPERATOR: 20,
    UserRole.ADMIN: 30,
    UserRole.OWNER: 40,
}


@dataclass(frozen=True, slots=True)
class Principal:
    user: User
    auth_type: Literal["jwt", "api_key"]
    scopes: tuple[str, ...]

    @property
    def role(self) -> UserRole:
        return self.user.role

    def has_scope(self, scope: str) -> bool:
        return not self.scopes or "*" in self.scopes or scope in self.scopes


async def _user_from_jwt(db: AsyncSession, token: str) -> Principal:
    claims = decode_access_token(token)
    user = await db.get(User, claims.subject)
    if user is None or not user.is_active:
        raise UnauthorizedError("The account is inactive or no longer exists")
    return Principal(user=user, auth_type="jwt", scopes=claims.scopes)


async def _user_from_api_key(db: AsyncSession, presented: str) -> Principal:
    digest = hash_api_key(presented)
    result = await db.execute(
        select(ApiKey).where(ApiKey.key_hash == digest, ApiKey.revoked_at.is_(None))
    )
    key = result.scalar_one_or_none()
    now = datetime.now(UTC)
    if key is None:
        raise UnauthorizedError("Invalid API key")
    if key.expires_at is not None and key.expires_at <= now:
        raise UnauthorizedError("The API key has expired")

    user = await db.get(User, key.user_id)
    if user is None or not user.is_active:
        raise UnauthorizedError("The account is inactive or no longer exists")

    if key.last_used_at is None or key.last_used_at < now - timedelta(minutes=1):
        key.last_used_at = now
        await db.commit()

    return Principal(user=user, auth_type="api_key", scopes=tuple(key.scopes or []))


async def get_principal(
    request: Request,
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)] = None,
    api_key: Annotated[str | None, Depends(_api_key_scheme)] = None,
) -> Principal:
    """Authenticate a request via Bearer JWT or X-API-Key."""
    principal: Principal | None = None
    if credentials is not None and credentials.scheme.lower() == "bearer":
        principal = await _user_from_jwt(db, credentials.credentials)
    elif api_key:
        principal = await _user_from_api_key(db, api_key)

    if principal is None:
        raise UnauthorizedError("Provide a Bearer token or an X-API-Key header")

    request.state.principal = principal
    return principal


PrincipalDep = Annotated[Principal, Depends(get_principal)]


async def principal_from_token(token: str, db: AsyncSession) -> Principal:
    """Authenticate a JWT supplied out-of-band (e.g. an EventSource query param)."""
    return await _user_from_jwt(db, token)


async def principal_from_request(request: Request, db: AsyncSession) -> Principal:
    """Authenticate from the standard headers, for routes that parse them manually."""
    credentials = await _bearer_scheme(request)
    api_key = await _api_key_scheme(request)
    return await get_principal(request, db, credentials, api_key)


async def get_current_user(principal: PrincipalDep) -> User:
    return principal.user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_role(
    minimum: UserRole, *, scope: str | None = None
) -> Callable[[Principal], Coroutine[Any, Any, Principal]]:
    """Return a dependency enforcing a minimum role and optional API-key scope."""

    async def _checker(principal: PrincipalDep) -> Principal:
        if _ROLE_RANK[principal.role] < _ROLE_RANK[minimum]:
            raise PermissionDeniedError(f"This action requires the {minimum.value} role or higher")
        if (
            scope is not None
            and principal.auth_type == "api_key"
            and not principal.has_scope(scope)
        ):
            raise PermissionDeniedError(f"The API key is missing the '{scope}' scope")
        return principal

    return _checker


ViewerPrincipal = Annotated[Principal, Depends(require_role(UserRole.VIEWER))]
OperatorPrincipal = Annotated[Principal, Depends(require_role(UserRole.OPERATOR))]
AdminPrincipal = Annotated[Principal, Depends(require_role(UserRole.ADMIN))]
OwnerPrincipal = Annotated[Principal, Depends(require_role(UserRole.OWNER))]


async def get_optional_user(
    request: Request,
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)] = None,
    api_key: Annotated[str | None, Depends(_api_key_scheme)] = None,
) -> User | None:
    """Best-effort authentication, useful for public endpoints with extras."""
    try:
        if credentials is not None or api_key:
            principal = await get_principal(request, db, credentials, api_key)
            return principal.user
    except UnauthorizedError:
        return None
    return None


async def parse_uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        from app.core.exceptions import BadRequestError

        raise BadRequestError("Malformed identifier") from exc
