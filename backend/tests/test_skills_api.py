"""Integration tests for the skill API, assignment and execution history."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest
from httpx import AsyncClient

from app.db.enums import UserRole
from app.services.users import create_user

pytestmark = pytest.mark.integration

SKILL_MD = """---
name: Local Deploy
description: Deploy one service and verify it is healthy.
version: 1.0.0
---

# Deploy

1. Check the health endpoint.
2. Deploy.
"""


async def _token(client: AsyncClient, email: str, role: UserRole) -> dict[str, str]:
    from app.db.session import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        await create_user(
            session, email=email, password="Str0ng-Passw0rd!", role=role, display_name="T"
        )
        await session.commit()
    response = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "Str0ng-Passw0rd!"}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture
async def owner(app_client: AsyncClient) -> dict[str, str]:
    return await _token(app_client, "skill-owner@example.com", UserRole.OWNER)


@pytest.fixture
async def viewer(app_client: AsyncClient) -> dict[str, str]:
    return await _token(app_client, "skill-viewer@example.com", UserRole.VIEWER)


@pytest.fixture(autouse=True)
async def seeded() -> AsyncIterator[None]:
    from app.db.session import get_session_factory
    from app.services.agents import sync_tool_registry
    from app.services.skills import sync_builtin_skills
    from app.tools.builtin import BUILTIN_TOOLS

    factory = get_session_factory()
    async with factory() as session:
        await sync_tool_registry(session, [tool() for tool in BUILTIN_TOOLS])
        await sync_builtin_skills(session)
        await session.commit()
    yield


async def _builtin_skill(client: AsyncClient, headers: dict[str, str]) -> dict[str, Any]:
    response = await client.get(
        "/api/v1/skills/search", headers=headers, params={"q": "code-review"}
    )
    assert response.status_code == 200, response.text
    results = [item for item in response.json() if item["slug"] == "code-review"]
    assert results, response.text
    return results[0]


async def _create_agent(client: AsyncClient, headers: dict[str, str], slug: str) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/agents",
        headers=headers,
        json={"slug": slug, "name": slug.replace("-", " ").title()},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_builtin_skills_are_installed(app_client: AsyncClient, owner) -> None:
    response = await app_client.get("/api/v1/skills", headers=owner)
    assert response.status_code == 200, response.text
    slugs = {item["slug"] for item in response.json()}
    assert {
        "code-review",
        "security-audit",
        "test-authoring",
        "bug-repro",
        "report-writing",
    } <= slugs
    assert all(item["is_system"] for item in response.json())


async def test_skill_detail_returns_body_and_files(app_client: AsyncClient, owner) -> None:
    skill = await _builtin_skill(app_client, owner)
    detail = await app_client.get(f"/api/v1/skills/{skill['id']}", headers=owner)
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert "# Skill: Code Review" in body["body"]
    assert "SKILL.md" in body["files"]
    assert body["assigned_agent_ids"] == []


async def test_create_custom_skill(app_client: AsyncClient, owner) -> None:
    created = await app_client.post(
        "/api/v1/skills",
        headers=owner,
        json={
            "slug": "custom-deploy",
            "name": "Custom Deploy",
            "description": "Deploy the custom service.",
            "version": "0.1.0",
            "body": "1. Do the deploy.",
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["source"] == "custom"
    assert body["version"] == "0.1.0"
    assert "Do the deploy" in body["body"]

    duplicate = await app_client.post(
        "/api/v1/skills",
        headers=owner,
        json={"slug": "custom-deploy", "name": "Again", "body": "x"},
    )
    assert duplicate.status_code == 409


async def test_create_skill_rejects_invalid_metadata(app_client: AsyncClient, owner) -> None:
    response = await app_client.post(
        "/api/v1/skills",
        headers=owner,
        json={
            "slug": "bad-version",
            "name": "Bad",
            "description": "Has a non-semver version.",
            "version": "v1",
            "body": "x",
        },
    )
    assert response.status_code == 422
    assert "semver" in response.text


async def test_validate_skill_accepts_content(app_client: AsyncClient, owner) -> None:
    ok = await app_client.post("/api/v1/skills/validate", headers=owner, json={"content": SKILL_MD})
    assert ok.status_code == 200
    assert ok.json()["valid"] is True
    assert ok.json()["version"] == "1.0.0"
    assert ok.json()["warnings"], "a skill without allowed-tools should warn"

    broken = await app_client.post(
        "/api/v1/skills/validate", headers=owner, json={"content": "no frontmatter"}
    )
    assert broken.status_code == 200
    assert broken.json()["valid"] is False
    assert broken.json()["errors"]


async def test_validate_skill_rejects_path_escape(app_client: AsyncClient, owner) -> None:
    response = await app_client.post(
        "/api/v1/skills/validate", headers=owner, json={"path": "../../etc"}
    )
    assert response.status_code == 200
    assert response.json()["valid"] is False
    assert "escapes" in response.json()["errors"][0]


async def test_enable_disable_and_delete(app_client: AsyncClient, owner) -> None:
    created = (
        await app_client.post(
            "/api/v1/skills",
            headers=owner,
            json={"slug": "toggle-me", "name": "Toggle", "description": "d", "body": "b"},
        )
    ).json()

    disabled = await app_client.patch(
        f"/api/v1/skills/{created['id']}", headers=owner, json={"enabled": False}
    )
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False

    deleted = await app_client.delete(f"/api/v1/skills/{created['id']}", headers=owner)
    assert deleted.status_code == 204

    missing = await app_client.get(f"/api/v1/skills/{created['id']}", headers=owner)
    assert missing.status_code == 404


async def test_builtin_skill_cannot_be_deleted(app_client: AsyncClient, owner) -> None:
    skill = await _builtin_skill(app_client, owner)
    response = await app_client.delete(f"/api/v1/skills/{skill['id']}", headers=owner)
    assert response.status_code == 409
    assert "disable" in response.text


async def test_assign_and_unassign_skill(app_client: AsyncClient, owner) -> None:
    skill = await _builtin_skill(app_client, owner)
    agent = await _create_agent(app_client, owner, "skill-holder")

    assigned = await app_client.post(
        f"/api/v1/skills/{skill['id']}/assign/{agent['id']}", headers=owner
    )
    assert assigned.status_code == 200, assigned.text
    assert str(agent["id"]) in assigned.json()["assigned_agent_ids"]

    detail = await app_client.get(f"/api/v1/agents/{agent['id']}", headers=owner)
    assert detail.json()["skills"], detail.text

    removed = await app_client.delete(
        f"/api/v1/skills/{skill['id']}/assign/{agent['id']}", headers=owner
    )
    assert removed.status_code == 204

    again = await app_client.get(f"/api/v1/skills/{skill['id']}", headers=owner)
    assert again.json()["assigned_agent_ids"] == []


async def test_assign_unknown_agent_is_404(app_client: AsyncClient, owner) -> None:
    skill = await _builtin_skill(app_client, owner)
    response = await app_client.post(
        f"/api/v1/skills/{skill['id']}/assign/{uuid4()}", headers=owner
    )
    assert response.status_code == 404


async def test_viewer_cannot_create_skill(app_client: AsyncClient, viewer) -> None:
    response = await app_client.post(
        "/api/v1/skills",
        headers=viewer,
        json={"slug": "viewer-skill", "name": "V", "body": "b"},
    )
    assert response.status_code == 403


async def test_sync_reports_install_and_idempotency(
    app_client: AsyncClient, owner, tmp_path
) -> None:
    from app.core.config import get_settings

    directory = get_settings().skills_root / "synced-skill"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")

    first = await app_client.post("/api/v1/skills/sync", headers=owner)
    assert first.status_code == 200, first.text
    assert "synced-skill" in first.json()["installed"]

    second = await app_client.post("/api/v1/skills/sync", headers=owner)
    assert second.status_code == 200
    assert "synced-skill" in second.json()["unchanged"]


async def test_version_listing_and_execution_history(app_client: AsyncClient, owner) -> None:
    from app.db.session import get_session_factory
    from app.services import skills as skill_service

    skill = await _builtin_skill(app_client, owner)
    versions = await app_client.get(f"/api/v1/skills/{skill['id']}/versions", headers=owner)
    assert versions.status_code == 200
    assert [item["version"] for item in versions.json()] == [skill["version"]]

    agent = await _create_agent(app_client, owner, "history-agent")
    await app_client.post(f"/api/v1/skills/{skill['id']}/assign/{agent['id']}", headers=owner)

    factory = get_session_factory()
    async with factory() as session:
        skill_service.get_skill_by_slug  # noqa: B018 - import check
        await skill_service.load_skill_for_agent(session, agent_id=agent["id"], slug=skill["slug"])
        await session.commit()

    history = await app_client.get(f"/api/v1/skills/{skill['id']}/executions", headers=owner)
    assert history.status_code == 200, history.text
    rows = history.json()
    assert len(rows) == 1
    assert rows[0]["event"] == "load"
    assert rows[0]["ok"] is True
    assert rows[0]["agent_id"] == agent["id"]


async def test_loading_unassigned_skill_fails(app_client: AsyncClient, owner) -> None:
    from app.core.exceptions import NotFoundError
    from app.db.session import get_session_factory
    from app.services import skills as skill_service

    agent = await _create_agent(app_client, owner, "no-skills-agent")
    factory = get_session_factory()
    async with factory() as session:
        with pytest.raises(NotFoundError):
            await skill_service.load_skill_for_agent(
                session, agent_id=agent["id"], slug="code-review"
            )


async def test_import_rejects_non_repository_url(app_client: AsyncClient, owner) -> None:
    response = await app_client.post(
        "/api/v1/skills/import", headers=owner, json={"repo_url": "file:///etc/passwd"}
    )
    assert response.status_code == 422
    assert "URL" in response.text
