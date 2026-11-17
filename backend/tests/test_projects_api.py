"""Integration tests for the project/workspace API and coding sessions."""

from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import AsyncClient

from app.db.enums import UserRole
from app.services.users import create_user

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
async def workspace_root(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Path]:
    root = Path(tempfile.mkdtemp(prefix="agentos-workspaces-"))
    os.environ["WORKSPACE_ROOT"] = str(root)

    from app.core.config import get_settings

    get_settings.cache_clear()
    yield root
    get_settings.cache_clear()


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
async def admin(app_client: AsyncClient) -> dict[str, str]:
    return await _token(app_client, "proj-admin@example.com", UserRole.ADMIN)


@pytest.fixture
async def viewer(app_client: AsyncClient) -> dict[str, str]:
    return await _token(app_client, "proj-viewer@example.com", UserRole.VIEWER)


async def _create_project(client: AsyncClient, headers: dict[str, str], slug: str = "demo") -> dict:
    response = await client.post(
        "/api/v1/projects",
        headers=headers,
        json={"slug": slug, "name": "Demo", "description": "d"},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_create_project_makes_workspace(
    app_client: AsyncClient, admin: dict[str, str], workspace_root: Path
) -> None:
    project = await _create_project(app_client, admin)
    assert project["slug"] == "demo"
    created = Path(project["workspace_path"])
    assert await asyncio.to_thread(created.is_dir)
    assert await asyncio.to_thread(lambda: created.parent == workspace_root.resolve())


async def test_duplicate_slug_is_rejected(app_client: AsyncClient, admin: dict[str, str]) -> None:
    await _create_project(app_client, admin, slug="dupe")
    response = await app_client.post(
        "/api/v1/projects",
        headers=admin,
        json={"slug": "dupe", "name": "Again"},
    )
    assert response.status_code == 409


async def test_invalid_slug_is_rejected(app_client: AsyncClient, admin: dict[str, str]) -> None:
    response = await app_client.post(
        "/api/v1/projects", headers=admin, json={"slug": "../etc", "name": "Bad"}
    )
    assert response.status_code == 422


async def test_viewer_can_read_projects(
    app_client: AsyncClient, admin: dict[str, str], viewer: dict[str, str]
) -> None:
    project = await _create_project(app_client, admin, slug="readable")
    listing = await app_client.get("/api/v1/projects", headers=viewer)
    assert listing.status_code == 200
    assert any(item["id"] == project["id"] for item in listing.json())


async def test_viewer_cannot_create_project(
    app_client: AsyncClient, viewer: dict[str, str]
) -> None:
    response = await app_client.post(
        "/api/v1/projects", headers=viewer, json={"slug": "nope", "name": "N"}
    )
    assert response.status_code == 403


async def test_file_write_read_list_delete(app_client: AsyncClient, admin: dict[str, str]) -> None:
    project = await _create_project(app_client, admin, slug="files")
    base = f"/api/v1/projects/{project['id']}"

    written = await app_client.put(
        f"{base}/file", headers=admin, json={"path": "src/main.py", "content": "print(1)\n"}
    )
    assert written.status_code == 200, written.text
    assert written.json()["created"] is True

    read = await app_client.get(f"{base}/file", headers=admin, params={"path": "src/main.py"})
    assert read.status_code == 200
    assert read.json()["content"] == "print(1)\n"

    tree = await app_client.get(f"{base}/files", headers=admin)
    assert tree.status_code == 200
    assert any(entry["path"] == "src/main.py" for entry in tree.json()["entries"])

    deleted = await app_client.delete(f"{base}/file", headers=admin, params={"path": "src/main.py"})
    assert deleted.status_code == 204

    missing = await app_client.get(f"{base}/file", headers=admin, params={"path": "src/main.py"})
    assert missing.status_code == 404


async def test_file_traversal_is_refused(app_client: AsyncClient, admin: dict[str, str]) -> None:
    project = await _create_project(app_client, admin, slug="escape")
    base = f"/api/v1/projects/{project['id']}"

    write = await app_client.put(
        f"{base}/file", headers=admin, json={"path": "../escape.txt", "content": "x"}
    )
    assert write.status_code == 403

    read = await app_client.get(f"{base}/file", headers=admin, params={"path": "/etc/passwd"})
    assert read.status_code in {403, 404}
    assert "root:" not in read.text


async def test_search_endpoint(app_client: AsyncClient, admin: dict[str, str]) -> None:
    project = await _create_project(app_client, admin, slug="searchable")
    base = f"/api/v1/projects/{project['id']}"
    await app_client.put(
        f"{base}/file", headers=admin, json={"path": "a.py", "content": "def needle():\n"}
    )
    response = await app_client.post(
        f"{base}/search", headers=admin, json={"pattern": "def needle"}
    )
    assert response.status_code == 200
    assert response.json()["count"] == 1


async def test_workspace_status(app_client: AsyncClient, admin: dict[str, str]) -> None:
    project = await _create_project(app_client, admin, slug="status")
    response = await app_client.get(f"/api/v1/projects/{project['id']}/workspace", headers=admin)
    assert response.status_code == 200
    body = response.json()
    assert body["exists"] is True
    assert body["repositories"] == 0


async def test_run_command_requires_admin(app_client: AsyncClient, admin: dict[str, str]) -> None:
    project = await _create_project(app_client, admin, slug="cmd")
    operator = await _token(app_client, "proj-op@example.com", UserRole.OPERATOR)
    response = await app_client.post(
        f"/api/v1/projects/{project['id']}/commands",
        headers=operator,
        json={"command": "echo hi"},
    )
    assert response.status_code == 403

    allowed = await app_client.post(
        f"/api/v1/projects/{project['id']}/commands",
        headers=admin,
        json={"command": "echo hi"},
    )
    assert allowed.status_code == 200, allowed.text
    assert "hi" in allowed.json()["stdout"]
    assert allowed.json()["sandbox"] in {"local", "docker"}


async def test_engines_endpoint(app_client: AsyncClient, admin: dict[str, str]) -> None:
    response = await app_client.get("/api/v1/projects/engines", headers=admin)
    assert response.status_code == 200
    names = {item["name"] for item in response.json()}
    assert {"opencode", "shell"} <= names


async def test_coding_run_records_session(app_client: AsyncClient, admin: dict[str, str]) -> None:
    project = await _create_project(app_client, admin, slug="codings")
    base = f"/api/v1/projects/{project['id']}"

    run = await app_client.post(
        f"{base}/coding/run",
        headers=admin,
        json={"instruction": "echo recorded", "engine": "shell", "timeout_seconds": 30},
    )
    assert run.status_code == 200, run.text
    body = run.json()
    assert body["engine"] == "shell"
    assert body["ok"] is True

    listing = await app_client.get(f"{base}/coding/sessions", headers=admin)
    assert listing.status_code == 200
    sessions = listing.json()
    assert len(sessions) == 1
    assert sessions[0]["engine"] == "shell"
    assert sessions[0]["status"] == "completed"

    detail = await app_client.get(f"{base}/coding/sessions/{sessions[0]['id']}", headers=admin)
    assert detail.status_code == 200
    assert detail.json()["instruction"] == "echo recorded"


async def test_coding_session_scoped_to_project(
    app_client: AsyncClient, admin: dict[str, str]
) -> None:
    first = await _create_project(app_client, admin, slug="one")
    second = await _create_project(app_client, admin, slug="two")
    await app_client.post(
        f"/api/v1/projects/{first['id']}/coding/run",
        headers=admin,
        json={"instruction": "echo x", "engine": "shell", "timeout_seconds": 30},
    )
    sessions = (
        await app_client.get(f"/api/v1/projects/{first['id']}/coding/sessions", headers=admin)
    ).json()
    response = await app_client.get(
        f"/api/v1/projects/{second['id']}/coding/sessions/{sessions[0]['id']}",
        headers=admin,
    )
    assert response.status_code == 404


async def test_repository_clone_rejects_non_https(
    app_client: AsyncClient, admin: dict[str, str]
) -> None:
    project = await _create_project(app_client, admin, slug="repo-bad")
    response = await app_client.post(
        f"/api/v1/projects/{project['id']}/repositories",
        headers=admin,
        json={"url": "git@github.com:o/r.git"},
    )
    assert response.status_code == 422


async def test_repository_clone_rejects_embedded_credentials(
    app_client: AsyncClient, admin: dict[str, str]
) -> None:
    project = await _create_project(app_client, admin, slug="repo-creds")
    response = await app_client.post(
        f"/api/v1/projects/{project['id']}/repositories",
        headers=admin,
        json={"url": "https://user:token@github.com/o/r.git"},
    )
    assert response.status_code == 422


async def test_project_audit_entries(app_client: AsyncClient, admin: dict[str, str]) -> None:
    from sqlalchemy import select

    from app.db.models import AuditLog
    from app.db.session import get_session_factory

    project = await _create_project(app_client, admin, slug="audited")
    await app_client.put(
        f"/api/v1/projects/{project['id']}/file",
        headers=admin,
        json={"path": "a.txt", "content": "x"},
    )
    factory = get_session_factory()
    async with factory() as session:
        actions = (await session.execute(select(AuditLog.action))).scalars().all()
    assert "project.created" in actions
    assert "project.file_written" in actions
