"""Integration tests for memory storage, retrieval, isolation and the API."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from httpx import AsyncClient

from app.core.config import get_settings
from app.db.enums import MemoryLayer, UserRole
from app.db.session import get_session_factory
from app.services import memory as memory_service
from app.services import users as user_service

pytestmark = pytest.mark.integration


async def _token(client: AsyncClient, email: str, role: UserRole) -> dict[str, Any]:
    factory = get_session_factory()
    async with factory() as session:
        user = await user_service.create_user(
            session, email=email, password="Str0ng-Passw0rd!", role=role, display_name="M"
        )
        await session.commit()
        user_id = str(user.id)
    response = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "Str0ng-Passw0rd!"}
    )
    assert response.status_code == 200, response.text
    return {
        "Authorization": f"Bearer {response.json()['access_token']}",
        "user_id": user_id,
    }


@pytest.fixture
async def owner(app_client: AsyncClient) -> dict[str, Any]:
    return await _token(app_client, "mem-owner@example.com", UserRole.OWNER)


@pytest.fixture
async def other(app_client: AsyncClient) -> dict[str, Any]:
    return await _token(app_client, "mem-other@example.com", UserRole.OPERATOR)


@pytest.fixture(autouse=True)
async def seeded() -> AsyncIterator[None]:
    from app.services.agents import sync_tool_registry
    from app.tools.builtin import BUILTIN_TOOLS

    factory = get_session_factory()
    async with factory() as session:
        await sync_tool_registry(session, [tool() for tool in BUILTIN_TOOLS])
        await session.commit()
    yield


async def _agent(client: AsyncClient, headers: dict[str, Any], slug: str) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/agents", headers=headers, json={"slug": slug, "name": slug.title()}
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _project(client: AsyncClient, headers: dict[str, Any], slug: str) -> dict[str, Any]:
    """Projects have no HTTP surface yet, so seed one directly."""
    from app.db.models import Project

    factory = get_session_factory()
    async with factory() as session:
        project = Project(
            slug=slug,
            name=slug.title(),
            owner_id=headers["user_id"],
            workspace_path=f"/data/workspaces/{slug}",
        )
        session.add(project)
        await session.commit()
        return {"id": str(project.id), "slug": project.slug}


async def _remember(db, *, content: str, key: str | None = None, **kwargs: Any):
    return await memory_service.write_memory(
        db, memory_service.MemoryWrite(content=content, key=key, **kwargs)
    )


async def test_write_and_fetch_memory(app_client: AsyncClient, owner) -> None:
    created = await app_client.post(
        "/api/v1/memory",
        headers=owner,
        json={
            "content": "The staging cluster runs PostgreSQL 16 with pgvector.",
            "layer": "semantic",
            "key": "infra/staging-db",
            "importance": 0.8,
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["layer"] == "semantic"
    assert body["version"] == 1
    assert body["user_id"] == owner["user_id"]
    assert body["access_count"] == 0

    fetched = await app_client.get(f"/api/v1/memory/{body['id']}", headers=owner)
    assert fetched.status_code == 200
    assert fetched.json()["content"].startswith("The staging cluster")


async def test_write_rejects_secrets(app_client: AsyncClient, owner) -> None:
    response = await app_client.post(
        "/api/v1/memory",
        headers=owner,
        json={"content": "Deploy with sk-abcdefghijklmnopqrstuvwx and you are done."},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "memory_sensitive_content"

    leaked = await app_client.get("/api/v1/memory", headers=owner)
    assert leaked.json() == []


async def test_dedup_bumps_access_and_new_content_versions(app_client: AsyncClient, owner) -> None:
    first = (
        await app_client.post(
            "/api/v1/memory",
            headers=owner,
            json={"content": "Deploys go through Argo CD.", "key": "deploy/path"},
        )
    ).json()

    same = (
        await app_client.post(
            "/api/v1/memory",
            headers=owner,
            json={"content": "Deploys go through Argo CD.", "key": "deploy/path"},
        )
    ).json()
    assert same["id"] == first["id"]
    assert same["version"] == 1
    assert same["access_count"] == 1

    changed = (
        await app_client.post(
            "/api/v1/memory",
            headers=owner,
            json={"content": "Deploys now go through Flux CD.", "key": "deploy/path"},
        )
    ).json()
    assert changed["id"] != first["id"]
    assert changed["version"] == 2
    assert changed["parent_memory_id"] == first["id"]

    versions = await app_client.get(f"/api/v1/memory/{changed['id']}/versions", headers=owner)
    assert [item["version"] for item in versions.json()] == [1, 2]


async def test_search_prefers_lexical_match(app_client: AsyncClient, owner) -> None:
    for content in (
        "Argon2id is used to hash passwords.",
        "The dashboard renders charts with ECharts.",
        "Redis Streams carry task events between workers.",
    ):
        response = await app_client.post("/api/v1/memory", headers=owner, json={"content": content})
        assert response.status_code == 201, response.text

    hits = await app_client.post(
        "/api/v1/memory/search",
        headers=owner,
        json={"query": "Redis Streams task events", "use_vector": False},
    )
    assert hits.status_code == 200, hits.text
    results = hits.json()
    assert results
    assert results[0]["memory"]["content"].startswith("Redis Streams")
    assert results[0]["ranking"]["score"] > 0
    assert results[0]["ranking"]["lexical"] > 0
    assert results[0]["ranking"]["vector"] is None


async def test_search_is_isolated_by_agent(app_client: AsyncClient, owner) -> None:
    agent_a = await _agent(app_client, owner, "agent-a")
    agent_b = await _agent(app_client, owner, "agent-b")
    await app_client.post(
        "/api/v1/memory",
        headers=owner,
        json={"content": "Agent A prefers terse reports.", "agent_id": agent_a["id"]},
    )
    await app_client.post(
        "/api/v1/memory",
        headers=owner,
        json={"content": "Agent B prefers long reports.", "agent_id": agent_b["id"]},
    )

    isolated = await app_client.post(
        "/api/v1/memory/search",
        headers=owner,
        json={"query": "agent prefers reports", "agent_id": agent_a["id"], "use_vector": False},
    )
    contents = [hit["memory"]["content"] for hit in isolated.json()]
    assert contents == ["Agent A prefers terse reports."]


async def test_project_sessions_do_not_cross(app_client: AsyncClient, owner) -> None:
    project_a = await _project(app_client, owner, "project-a")
    project_b = await _project(app_client, owner, "project-b")
    await app_client.post(
        "/api/v1/memory",
        headers=owner,
        json={"content": "Project A uses pytest.", "project_id": project_a["id"]},
    )
    await app_client.post(
        "/api/v1/memory",
        headers=owner,
        json={"content": "Project B uses unittest.", "project_id": project_b["id"]},
    )

    scoped = await app_client.post(
        "/api/v1/memory/search",
        headers=owner,
        json={"query": "project uses", "project_id": project_b["id"], "use_vector": False},
    )
    assert [hit["memory"]["content"] for hit in scoped.json()] == ["Project B uses unittest."]


async def test_memory_isolation_between_users(app_client: AsyncClient, owner, other) -> None:
    created = (
        await app_client.post(
            "/api/v1/memory",
            headers=owner,
            json={"content": "Owner-only fact about canary deploys."},
        )
    ).json()

    listing = await app_client.get("/api/v1/memory", headers=other)
    assert created["id"] not in {item["id"] for item in listing.json()}

    search = await app_client.post(
        "/api/v1/memory/search",
        headers=other,
        json={"query": "canary deploys", "use_vector": False},
    )
    assert search.json() == []


async def test_context_respects_budget(app_client: AsyncClient, owner) -> None:
    for index in range(6):
        await app_client.post(
            "/api/v1/memory",
            headers=owner,
            json={"content": f"Fact number {index}: rolling deploys are required. " + "x" * 200},
        )

    response = await app_client.post(
        "/api/v1/memory/context",
        headers=owner,
        json={"query": "rolling deploys", "limit": 10, "use_vector": False},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["items"] >= 1
    assert body["context"]
    assert len(body["context"]) <= get_settings().memory_max_context_chars


async def test_summary_is_deterministic(app_client: AsyncClient, owner) -> None:
    for content in ("Alpha happens first.", "Beta happens second.", "Gamma happens third."):
        await app_client.post("/api/v1/memory", headers=owner, json={"content": content})

    first = await app_client.post(
        "/api/v1/memory/summary",
        headers=owner,
        json={"query": "happens", "use_vector": False},
    )
    second = await app_client.post(
        "/api/v1/memory/summary",
        headers=owner,
        json={"query": "happens", "use_vector": False},
    )
    assert first.json() == second.json()
    assert first.json()["items"] == 3
    assert "Alpha" in first.json()["summary"]


async def test_update_metadata_in_place_and_content_as_new_version(
    app_client: AsyncClient, owner
) -> None:
    created = (
        await app_client.post(
            "/api/v1/memory",
            headers=owner,
            json={"content": "Cache TTL is 60 seconds.", "key": "cache/ttl"},
        )
    ).json()

    bumped = await app_client.patch(
        f"/api/v1/memory/{created['id']}", headers=owner, json={"importance": 0.95}
    )
    assert bumped.status_code == 200
    assert bumped.json()["importance"] == 0.95
    assert bumped.json()["version"] == 1

    revised = await app_client.patch(
        f"/api/v1/memory/{created['id']}",
        headers=owner,
        json={"content": "Cache TTL is 30 seconds."},
    )
    assert revised.json()["version"] == 2
    assert revised.json()["parent_memory_id"] == created["id"]

    rejected = await app_client.patch(
        f"/api/v1/memory/{created['id']}",
        headers=owner,
        json={"content": "token=abc123456789"},
    )
    assert rejected.status_code == 422


async def test_delete_memory(app_client: AsyncClient, owner) -> None:
    created = (
        await app_client.post("/api/v1/memory", headers=owner, json={"content": "Delete me later."})
    ).json()
    response = await app_client.delete(f"/api/v1/memory/{created['id']}", headers=owner)
    assert response.status_code == 204
    assert (
        await app_client.get(f"/api/v1/memory/{created['id']}", headers=owner)
    ).status_code == 404


async def test_list_filters_by_layer_and_query(app_client: AsyncClient, owner) -> None:
    await app_client.post(
        "/api/v1/memory",
        headers=owner,
        json={"content": "User prefers Indonesian replies.", "layer": "agent"},
    )
    await app_client.post(
        "/api/v1/memory",
        headers=owner,
        json={"content": "Task finished at 14:00.", "layer": "episodic"},
    )

    prefs = await app_client.get("/api/v1/memory", headers=owner, params={"layer": "agent"})
    assert [item["layer"] for item in prefs.json()] == ["agent"]

    searched = await app_client.get("/api/v1/memory", headers=owner, params={"q": "14:00"})
    assert len(searched.json()) == 1

    layers = await app_client.get("/api/v1/memory/layers", headers=owner)
    assert "semantic" in layers.json()
    assert "working" in layers.json()


async def test_purge_expired_is_operator_only(app_client: AsyncClient, owner) -> None:
    viewer = await _token(app_client, "mem-viewer@example.com", UserRole.VIEWER)
    forbidden = await app_client.post("/api/v1/memory/purge-expired", headers=viewer)
    assert forbidden.status_code == 403

    factory = get_session_factory()
    async with factory() as session:
        expired = await memory_service.write_memory(
            session,
            memory_service.MemoryWrite(
                content="This working memory is stale.",
                layer=MemoryLayer.WORKING,
                source="test",
            ),
        )
        expired.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        await session.commit()
        expired_id = str(expired.id)

    purged = await app_client.post("/api/v1/memory/purge-expired", headers=owner)
    assert purged.status_code == 200, purged.text
    assert purged.json()["removed"] >= 1
    assert (await app_client.get(f"/api/v1/memory/{expired_id}", headers=owner)).status_code == 404


async def test_ttl_sets_expiry(app_client: AsyncClient, owner) -> None:
    created = (
        await app_client.post(
            "/api/v1/memory",
            headers=owner,
            json={"content": "Short-lived working note.", "layer": "working", "ttl_days": 2},
        )
    ).json()
    assert created["expires_at"] is not None


async def test_record_task_outcome_writes_episodic_memory(app_client: AsyncClient, owner) -> None:
    from app.db.enums import TaskStatus
    from app.db.models import Task

    agent = await _agent(app_client, owner, "outcome-agent")
    factory = get_session_factory()
    async with factory() as session:
        task = Task(
            title="Fix the flaky migration test",
            objective="Make the migration test deterministic.",
            status=TaskStatus.COMPLETED,
            assigned_agent_id=agent["id"],
            created_by=owner["user_id"],
            output={"content": "Pinned the seeded clock."},
        )
        session.add(task)
        await session.flush()
        memory = await memory_service.record_task_outcome(session, task)
        await session.commit()

    assert memory is not None
    assert memory.layer is MemoryLayer.EPISODIC
    assert "Fix the flaky migration test" in memory.content
    assert str(memory.agent_id) == agent["id"]
    assert str(memory.task_id) == str(task.id)


async def test_failed_task_outcome_is_not_recorded(app_client: AsyncClient, owner) -> None:
    from app.db.enums import TaskStatus
    from app.db.models import Task

    factory = get_session_factory()
    async with factory() as session:
        task = Task(
            title="Broken run",
            objective="Fail.",
            status=TaskStatus.FAILED,
            created_by=owner["user_id"],
            output={},
        )
        session.add(task)
        await session.flush()
        assert await memory_service.record_task_outcome(session, task) is None


async def test_vector_search_requires_embedding_configuration(
    app_client: AsyncClient, owner
) -> None:
    await app_client.post(
        "/api/v1/memory", headers=owner, json={"content": "Vectors are optional here."}
    )
    response = await app_client.post(
        "/api/v1/memory/search",
        headers=owner,
        json={"query": "vectors optional", "use_vector": True},
    )
    # Without an embedding provider configured the hybrid search degrades to lexical.
    assert response.status_code == 200, response.text
    assert response.json()


async def test_search_hits_mark_accessed(app_client: AsyncClient, owner) -> None:
    created = (
        await app_client.post(
            "/api/v1/memory", headers=owner, json={"content": "Kubernetes probes run every 15s."}
        )
    ).json()
    await app_client.post(
        "/api/v1/memory/search",
        headers=owner,
        json={"query": "kubernetes probes", "use_vector": False},
    )
    refreshed = await app_client.get(f"/api/v1/memory/{created['id']}", headers=owner)
    assert refreshed.json()["access_count"] == 1
    assert refreshed.json()["accessed_at"] is not None


async def test_unknown_memory_is_404(app_client: AsyncClient, owner) -> None:
    assert (await app_client.get(f"/api/v1/memory/{uuid4()}", headers=owner)).status_code == 404
