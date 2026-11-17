from __future__ import annotations

from uuid import uuid4

import pytest

from app.api.deps import Principal, require_role
from app.core.exceptions import PermissionDeniedError
from app.db.enums import UserRole
from app.db.models import User


def _principal(
    role: UserRole, *, auth_type: str = "jwt", scopes: tuple[str, ...] = ()
) -> Principal:
    user = User(
        id=uuid4(),
        email=f"{role.value}@example.com",
        display_name=role.value,
        role=role,
        is_active=True,
        hashed_password=None,
    )
    return Principal(user=user, auth_type=auth_type, scopes=scopes)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("role", "allowed"),
    [
        (UserRole.OWNER, True),
        (UserRole.ADMIN, True),
        (UserRole.OPERATOR, False),
        (UserRole.VIEWER, False),
    ],
)
async def test_require_admin_role(role: UserRole, allowed: bool) -> None:
    checker = require_role(UserRole.ADMIN)
    principal = _principal(role)
    if allowed:
        assert await checker(principal) is principal
    else:
        with pytest.raises(PermissionDeniedError):
            await checker(principal)


async def test_api_key_scope_enforced() -> None:
    checker = require_role(UserRole.VIEWER, scope="tasks:write")
    scoped = _principal(UserRole.VIEWER, auth_type="api_key", scopes=("tasks:write",))
    assert await checker(scoped) is scoped

    unscoped = _principal(UserRole.VIEWER, auth_type="api_key", scopes=("tasks:read",))
    with pytest.raises(PermissionDeniedError):
        await checker(unscoped)

    wildcard = _principal(UserRole.VIEWER, auth_type="api_key", scopes=("*",))
    assert await checker(wildcard) is wildcard
