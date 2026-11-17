from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.db.enums import UserRole
from app.services.users import create_user


@pytest.mark.integration
async def test_liveness(app_client: AsyncClient) -> None:
    response = await app_client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Request-ID"]


@pytest.mark.integration
@pytest.mark.redis
async def test_readiness(app_client: AsyncClient) -> None:
    response = await app_client.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["components"] == {"database": "ok", "redis": "ok"}


@pytest.mark.integration
async def test_meta(app_client: AsyncClient) -> None:
    response = await app_client.get("/api/v1/meta")
    assert response.status_code == 200
    body = response.json()
    assert body["phase"] == 2
    assert "calculate" in body["builtin_tools"]
    assert "task.created" in body["event_types"]


@pytest.mark.integration
async def test_login_and_me(app_client: AsyncClient, db_session) -> None:
    user = await create_user(
        db_session,
        email="Owner@Example.com",
        password="Str0ng-Passw0rd!",
        display_name="Owner",
        role=UserRole.OWNER,
    )
    await db_session.commit()

    bad = await app_client.post(
        "/api/v1/auth/login",
        json={"email": "owner@example.com", "password": "wrong-password"},
    )
    assert bad.status_code == 401
    assert bad.json()["error"]["code"] == "unauthorized"

    response = await app_client.post(
        "/api/v1/auth/login",
        json={"email": "owner@example.com", "password": "Str0ng-Passw0rd!"},
    )
    assert response.status_code == 200
    token = response.json()["access_token"]

    me = await app_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["id"] == str(user.id)
    assert me.json()["role"] == "owner"

    anonymous = await app_client.get("/api/v1/auth/me")
    assert anonymous.status_code == 401


@pytest.mark.integration
async def test_api_key_lifecycle(app_client: AsyncClient, db_session) -> None:
    await create_user(
        db_session,
        email="key@example.com",
        password="Str0ng-Passw0rd!",
        role=UserRole.ADMIN,
    )
    await db_session.commit()

    login = await app_client.post(
        "/api/v1/auth/login",
        json={"email": "key@example.com", "password": "Str0ng-Passw0rd!"},
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    created = await app_client.post(
        "/api/v1/auth/api-keys",
        json={"name": "ci", "scopes": ["read"]},
        headers=headers,
    )
    assert created.status_code == 201
    key = created.json()["key"]
    key_id = created.json()["id"]

    me = await app_client.get("/api/v1/auth/me", headers={"X-API-Key": key})
    assert me.status_code == 200
    assert me.json()["email"] == "key@example.com"

    listed = await app_client.get("/api/v1/auth/api-keys", headers=headers)
    assert listed.status_code == 200
    assert len(listed.json()) == 1
    assert "key" not in listed.json()[0]

    revoked = await app_client.delete(f"/api/v1/auth/api-keys/{key_id}", headers=headers)
    assert revoked.status_code == 204

    rejected = await app_client.get("/api/v1/auth/me", headers={"X-API-Key": key})
    assert rejected.status_code == 401


@pytest.mark.integration
async def test_api_key_cannot_create_api_key(app_client: AsyncClient, db_session) -> None:
    await create_user(
        db_session, email="a@example.com", password="Str0ng-Passw0rd!", role=UserRole.ADMIN
    )
    await db_session.commit()
    login = await app_client.post(
        "/api/v1/auth/login", json={"email": "a@example.com", "password": "Str0ng-Passw0rd!"}
    )
    token = login.json()["access_token"]
    created = await app_client.post(
        "/api/v1/auth/api-keys", json={"name": "x"}, headers={"Authorization": f"Bearer {token}"}
    )
    key = created.json()["key"]
    attempt = await app_client.post(
        "/api/v1/auth/api-keys", json={"name": "y"}, headers={"X-API-Key": key}
    )
    assert attempt.status_code == 401


@pytest.mark.integration
async def test_validation_error_shape(app_client: AsyncClient) -> None:
    response = await app_client.post("/api/v1/auth/login", json={"email": "not-an-email"})
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert body["error"]["request_id"]
