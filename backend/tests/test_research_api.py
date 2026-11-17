from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import AsyncClient

from app.core.config import get_settings
from app.db.enums import UserRole
from app.db.session import get_session_factory
from app.services.users import create_user

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
async def workspace_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def token(client: AsyncClient, email: str, role: UserRole) -> dict[str, str]:
    async with get_session_factory()() as db:
        await create_user(db, email=email, password="Str0ng-Passw0rd!", role=role)
        await db.commit()
    response = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "Str0ng-Passw0rd!"}
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def setup(client: AsyncClient) -> tuple[dict[str, str], dict[str, str], str]:
    author = await token(client, "research@example.com", UserRole.ADMIN)
    reviewer = await token(client, "reviewer@example.com", UserRole.OPERATOR)
    project = await client.post(
        "/api/v1/projects", headers=author, json={"slug": "research-test", "name": "Research"}
    )
    assert project.status_code == 201, project.text
    scope = await client.post(
        "/api/v1/research/scopes",
        headers=author,
        json={
            "project_id": project.json()["id"],
            "name": "Local source",
            "authorization": "Operator owns this local fixture",
            "allowed_tools": ["semgrep"],
        },
    )
    assert scope.status_code == 201, scope.text
    return author, reviewer, scope.json()["id"]


async def finding(client: AsyncClient, author: dict[str, str], scope: str) -> str:
    response = await client.post(
        "/api/v1/research/findings",
        headers=author,
        json={
            "scope_id": scope,
            "title": "Missing ownership check",
            "hypothesis": "A second user may access a record without an ownership check",
            "severity": "high",
            "cwe_id": "CWE-862",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def test_scope_verification_and_revocation(app_client: AsyncClient) -> None:
    author, reviewer, scope = await setup(app_client)
    response = await app_client.post(
        "/api/v1/research/findings",
        headers=author,
        json={"scope_id": scope, "title": "Lead", "hypothesis": "A possible authorization issue"},
    )
    assert response.status_code == 403
    assert (
        await app_client.post(f"/api/v1/research/scopes/{scope}/verify", headers=reviewer)
    ).status_code == 403
    assert (
        await app_client.post(f"/api/v1/research/scopes/{scope}/verify", headers=author)
    ).status_code == 200
    await finding(app_client, author, scope)
    assert (
        await app_client.post(f"/api/v1/research/scopes/{scope}/revoke", headers=author)
    ).status_code == 200
    response = await app_client.post(
        "/api/v1/research/scans", headers=author, json={"scope_id": scope, "tool": "semgrep"}
    )
    assert response.status_code == 403


async def test_confirmation_requires_current_independent_review(app_client: AsyncClient) -> None:
    author, reviewer, scope = await setup(app_client)
    await app_client.post(f"/api/v1/research/scopes/{scope}/verify", headers=author)
    fid = await finding(app_client, author, scope)
    base = f"/api/v1/research/findings/{fid}"

    async def move(status: str) -> int:
        return (
            await app_client.post(
                base + "/status",
                headers=author,
                json={"status": status, "rationale": "Controlled local verification"},
            )
        ).status_code

    assert await move("confirmed") == 409
    assert await move("validating") == 200
    assert await move("confirmed") == 409
    review_body = {"approved": True, "rationale": "Reproduced with two local test accounts"}
    assert (
        await app_client.post(base + "/reviews", headers=reviewer, json=review_body)
    ).status_code == 409
    ev = {
        "kind": "validation",
        "title": "Local reproduction",
        "content": "User B reads user A record; expected 403, actual 200",
    }
    assert (await app_client.post(base + "/evidence", headers=author, json=ev)).status_code == 201
    assert (
        await app_client.post(base + "/reviews", headers=author, json=review_body)
    ).status_code == 403
    assert (
        await app_client.post(base + "/reviews", headers=reviewer, json=review_body)
    ).status_code == 201
    ev["content"] = "Additional observed response evidence"
    await app_client.post(base + "/evidence", headers=author, json=ev)
    assert await move("confirmed") == 409
    await app_client.post(base + "/reviews", headers=reviewer, json=review_body)
    assert await move("confirmed") == 200
    assert (await app_client.post(base + "/evidence", headers=author, json=ev)).status_code == 409
    report = await app_client.get(base + "/report", headers=author)
    assert report.status_code == 200
    assert report.json()["externally_submitted"] is False
    assert "SHA-256:" in report.json()["markdown"]


async def test_scan_scope_and_path_checks_precede_execution(app_client: AsyncClient) -> None:
    author, _, scope = await setup(app_client)
    await app_client.post(f"/api/v1/research/scopes/{scope}/verify", headers=author)
    for payload, expected in [
        ({"tool": "forge"}, 403),
        ({"tool": "semgrep", "target_path": "../../etc"}, 403),
    ]:
        response = await app_client.post(
            "/api/v1/research/scans", headers=author, json={"scope_id": scope, **payload}
        )
        assert response.status_code == expected, response.text
