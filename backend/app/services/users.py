"""User lookup and authentication services."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, UnprocessableError
from app.core.security import hash_password, validate_password_strength, verify_password
from app.db.enums import UserRole
from app.db.models import User

_DUMMY_PASSWORD_HASH = hash_password("agentos-dummy-password-never-used")


def normalise_email(email: str) -> str:
    return email.strip().lower()


async def get_user_by_email(db: AsyncSession, email: str) -> User | None:
    result = await db.execute(select(User).where(User.email == normalise_email(email)))
    return result.scalar_one_or_none()


async def get_user_by_id(db: AsyncSession, user_id: UUID) -> User | None:
    return await db.get(User, user_id)


async def authenticate(db: AsyncSession, email: str, password: str) -> User | None:
    """Return the user when credentials are valid, otherwise ``None``.

    A dummy verification runs for unknown accounts so that response timing does
    not reveal whether an email address is registered.
    """
    user = await get_user_by_email(db, email)
    if user is None or not user.hashed_password or not user.is_active:
        verify_password(password, _DUMMY_PASSWORD_HASH)
        return None
    if not verify_password(password, user.hashed_password):
        return None
    return user


async def create_user(
    db: AsyncSession,
    *,
    email: str,
    password: str,
    display_name: str = "Operator",
    role: UserRole = UserRole.VIEWER,
) -> User:
    email = normalise_email(email)
    if await get_user_by_email(db, email) is not None:
        raise ConflictError("A user with this email already exists")
    try:
        validate_password_strength(password)
    except ValueError as exc:
        raise UnprocessableError(str(exc)) from exc
    user = User(
        email=email,
        display_name=display_name.strip() or "Operator",
        hashed_password=hash_password(password),
        role=role,
        is_active=True,
    )
    db.add(user)
    await db.flush()
    return user
