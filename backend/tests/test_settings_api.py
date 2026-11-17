from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.db.enums import UserRole
from app.db.models import SystemSetting
from app.db.session import get_session_factory
from app.services.users import create_user

pytestmark = pytest.mark.integration


async def headers(client: AsyncClient, email: str, role: UserRole) -> dict[str, str]:
    async with get_session_factory()() as db:
        await create_user(db, email=email, password="Str0ng-Passw0rd!", role=role)
        await db.commit()
    response = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "Str0ng-Passw0rd!"}
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def seed(db: object, key: str, value: dict, *, secret: bool = False) -> None:
    db.add(SystemSetting(key=key, value=value, is_secret=secret))  # type: ignore[attr-defined]
    await db.commit()  # type: ignore[attr-defined]


async def test_settings_require_admin(app_client: AsyncClient) -> None:
    operator = await headers(app_client, "settings-op@example.com", UserRole.OPERATOR)
    assert (await app_client.get("/api/v1/settings", headers=operator)).status_code == 403
    assert (await app_client.get("/api/v1/settings")).status_code == 401


async def test_settings_round_trip_and_secret_never_exposed(
    app_client: AsyncClient,
) -> None:
    admin = await headers(app_client, "settings-admin@example.com", UserRole.ADMIN)
    async with get_session_factory()() as db:
        db.add(SystemSetting(key="ui.theme", value={"value": "dark"}, description="Theme"))
        db.add(
            SystemSetting(
                key="provider.secret.token",
                value={"value": "not-returned"},
                is_secret=True,
            )
        )
        await db.commit()

    listing = await app_client.get("/api/v1/settings", headers=admin)
    assert listing.status_code == 200
    body = listing.json()
    keys = {item["key"]: item for item in body["items"]}
    assert keys["ui.theme"]["editable"] is True
    assert keys["provider.secret.token"]["editable"] is False
    assert body["environment"]["telegram_bot_configured"] is False

    updated = await app_client.patch(
        "/api/v1/settings/ui.theme", headers=admin, json={"value": {"value": "light"}}
    )
    assert updated.status_code == 200
    assert updated.json()["value"] == {"value": "light"}

    blocked = await app_client.patch(
        "/api/v1/settings/provider.secret.token", headers=admin, json={"value": {"value": "x"}}
    )
    assert blocked.status_code == 404
    unknown = await app_client.patch(
        "/api/v1/settings/not.a.key", headers=admin, json={"value": {}}
    )
    assert unknown.status_code == 404
