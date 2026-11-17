"""Integration tests for providers, agents, tools and task execution APIs."""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient

from app.db.enums import UserRole
from app.providers.base import LLMRequest, LLMResponse, Usage
from app.services.users import create_user

pytestmark = pytest.mark.integration


class FakeProvider:
    def __init__(self, responses: list[LLMResponse] | None = None) -> None:
        self.responses = responses or [
            LLMResponse(
                content="task finished",
                usage=Usage(prompt_tokens=12, completion_tokens=8, total_tokens=20),
                finish_reason="stop",
                model="fake-model",
            )
        ]
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("FakeProvider ran out of responses")
        return self.responses.pop(0)


async def _token(client: AsyncClient, email: str, role: UserRole) -> tuple[str, str]:
    from app.db.session import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        user = await create_user(
            session, email=email, password="Str0ng-Passw0rd!", role=role, display_name="Test"
        )
        await session.commit()
        user_id = str(user.id)

    response = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "Str0ng-Passw0rd!"}
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"], user_id


@pytest.fixture(autouse=True)
async def seeded_tools() -> AsyncIterator[None]:
    """Mirror the startup hook, which ASGITransport does not trigger."""
    from app.db.session import get_session_factory
    from app.services.agents import sync_tool_registry
    from app.tools.builtin import BUILTIN_TOOLS

    factory = get_session_factory()
    async with factory() as session:
        await sync_tool_registry(session, [tool() for tool in BUILTIN_TOOLS])
        await session.commit()
    yield


@pytest.fixture
async def owner(app_client: AsyncClient) -> AsyncIterator[dict[str, str]]:
    token, user_id = await _token(app_client, "owner2@example.com", UserRole.OWNER)
    yield {"Authorization": f"Bearer {token}", "user_id": user_id}


@pytest.fixture
async def viewer(app_client: AsyncClient) -> AsyncIterator[dict[str, str]]:
    token, user_id = await _token(app_client, "viewer2@example.com", UserRole.VIEWER)
    yield {"Authorization": f"Bearer {token}", "user_id": user_id}


async def _create_local_provider(client: AsyncClient, headers: dict[str, str]) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/providers",
        headers=headers,
        json={
            "slug": "fake-local",
            "name": "Fake local",
            "provider_type": "local",
            "base_url": "http://127.0.0.1:9/v1",
            "default_model": "fake-model",
            "enabled": True,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_provider_crud_and_secret_handling(
    app_client: AsyncClient, owner: dict[str, str]
) -> None:
    headers = {"Authorization": owner["Authorization"]}
    created = await app_client.post(
        "/api/v1/providers",
        headers=headers,
        json={
            "slug": "openai-main",
            "name": "OpenAI main",
            "provider_type": "openai",
            "api_key": "sk-super-secret-value",
            "default_model": "gpt-4o-mini",
            "enabled": True,
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["slug"] == "openai-main"
    assert body["api_key_hint"] == "…alue"
    assert "api_key" not in body
    assert "sk-super-secret-value" not in created.text

    provider_id = body["id"]
    listed = await app_client.get("/api/v1/providers", headers=headers)
    assert listed.status_code == 200
    assert len(listed.json()) == 1

    updated = await app_client.patch(
        f"/api/v1/providers/{provider_id}",
        headers=headers,
        json={"name": "Renamed", "clear_api_key": True},
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "Renamed"
    assert updated.json()["api_key_hint"] is None

    deleted = await app_client.delete(f"/api/v1/providers/{provider_id}", headers=headers)
    assert deleted.status_code == 204
    assert (await app_client.get("/api/v1/providers", headers=headers)).json() == []


async def test_provider_requires_admin(app_client: AsyncClient, viewer: dict[str, str]) -> None:
    headers = {"Authorization": viewer["Authorization"]}
    response = await app_client.post(
        "/api/v1/providers",
        headers=headers,
        json={"slug": "x", "name": "x", "provider_type": "local", "base_url": "http://127.0.0.1:1"},
    )
    assert response.status_code == 403


async def test_provider_duplicate_slug_conflicts(
    app_client: AsyncClient, owner: dict[str, str]
) -> None:
    headers = {"Authorization": owner["Authorization"]}
    await _create_local_provider(app_client, headers)
    duplicate = await app_client.post(
        "/api/v1/providers",
        headers=headers,
        json={
            "slug": "fake-local",
            "name": "dup",
            "provider_type": "local",
            "base_url": "http://127.0.0.1:9/v1",
        },
    )
    assert duplicate.status_code == 409


async def test_agent_crud_with_tool_assignments(
    app_client: AsyncClient, owner: dict[str, str]
) -> None:
    headers = {"Authorization": owner["Authorization"]}
    provider = await _create_local_provider(app_client, headers)

    created = await app_client.post(
        "/api/v1/agents",
        headers=headers,
        json={
            "slug": "analyst",
            "name": "Analyst",
            "system_prompt": "You analyse code.",
            "model_provider_id": provider["id"],
            "max_iterations": 5,
            "token_budget": 5000,
            "permissions": {"tools": ["tool:read"]},
        },
    )
    assert created.status_code == 201, created.text
    agent = created.json()
    assert agent["slug"] == "analyst"
    assert agent["model_name"] == "fake-model"
    assert agent["version"] == 1

    tool = await app_client.post(
        f"/api/v1/agents/{agent['id']}/tools",
        headers=headers,
        json={"tool_slug": "echo"},
    )
    assert tool.status_code == 200, tool.text
    assert tool.json()["enabled"] is True

    detail = await app_client.get(f"/api/v1/agents/{agent['id']}", headers=headers)
    assert len(detail.json()["tools"]) == 1

    patched = await app_client.patch(
        f"/api/v1/agents/{agent['id']}",
        headers=headers,
        json={"temperature": 0.7, "system_prompt": "New prompt"},
    )
    assert patched.status_code == 200
    assert patched.json()["temperature"] == 0.7
    assert patched.json()["version"] == 2

    removed = await app_client.delete(
        f"/api/v1/agents/{agent['id']}/tools/{tool.json()['tool_id']}", headers=headers
    )
    assert removed.status_code == 204

    deleted = await app_client.delete(f"/api/v1/agents/{agent['id']}", headers=headers)
    assert deleted.status_code == 204


async def test_agent_validation_rejects_absurd_limits(
    app_client: AsyncClient, owner: dict[str, str]
) -> None:
    headers = {"Authorization": owner["Authorization"]}
    response = await app_client.post(
        "/api/v1/agents",
        headers=headers,
        json={"slug": "bad", "name": "Bad", "max_iterations": 10_000},
    )
    assert response.status_code == 422


async def test_agent_without_model_cannot_run(
    app_client: AsyncClient, owner: dict[str, str]
) -> None:
    headers = {"Authorization": owner["Authorization"]}
    agent = (
        await app_client.post(
            "/api/v1/agents", headers=headers, json={"slug": "nomodel", "name": "No model"}
        )
    ).json()
    task = (
        await app_client.post(
            "/api/v1/tasks",
            headers=headers,
            json={
                "title": "t",
                "objective": "do it",
                "assigned_agent_id": agent["id"],
            },
        )
    ).json()
    response = await app_client.post(f"/api/v1/tasks/{task['id']}/run", headers=headers)
    assert response.status_code == 422


async def test_tool_registry_seeded_and_listed(
    app_client: AsyncClient, owner: dict[str, str]
) -> None:
    headers = {"Authorization": owner["Authorization"]}
    listed = await app_client.get("/api/v1/tools", headers=headers)
    assert listed.status_code == 200
    slugs = {tool["slug"] for tool in listed.json()}
    assert {"echo", "calculate", "now", "http_get"} <= slugs


async def test_tool_execute_endpoint(app_client: AsyncClient, owner: dict[str, str]) -> None:
    headers = {"Authorization": owner["Authorization"]}
    ok = await app_client.post(
        "/api/v1/tools/calculate/execute",
        headers=headers,
        json={"arguments": {"expression": "6 * 7"}},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["ok"] is True
    assert ok.json()["output"]["result"] == 42

    bad = await app_client.post(
        "/api/v1/tools/calculate/execute",
        headers=headers,
        json={"arguments": {"expression": "__import__('os')"}},
    )
    assert bad.status_code == 200
    assert bad.json()["ok"] is False


async def test_task_execution_persists_outcome(
    app_client: AsyncClient, owner: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = {"Authorization": owner["Authorization"]}
    provider = await _create_local_provider(app_client, headers)
    agent = (
        await app_client.post(
            "/api/v1/agents",
            headers=headers,
            json={
                "slug": "runner",
                "name": "Runner",
                "system_prompt": "run",
                "model_provider_id": provider["id"],
                "permissions": {"tools": ["tool:read"]},
                "token_budget": 10_000,
            },
        )
    ).json()

    fake = FakeProvider()
    monkeypatch.setattr("app.services.execution.provider_from_record", lambda *a, **k: fake)

    task = (
        await app_client.post(
            "/api/v1/tasks",
            headers=headers,
            json={
                "title": "Say hi",
                "objective": "Say hello",
                "assigned_agent_id": agent["id"],
                "budget_usd": "1.00",
            },
        )
    ).json()
    assert task["status"] == "pending"

    run = await app_client.post(f"/api/v1/tasks/{task['id']}/run", headers=headers)
    assert run.status_code == 200, run.text
    body = run.json()
    assert body["status"] == "completed"
    assert body["content"] == "task finished"
    assert body["total_tokens"] == 20
    assert Decimal(body["cost_usd"]) == Decimal("0")

    detail = await app_client.get(f"/api/v1/tasks/{task['id']}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["status"] == "completed"
    assert detail.json()["used_tokens"] == 20
    event_types = {event["event_type"] for event in detail.json()["events"]}
    assert {"task.created", "task.running", "task.completed"} <= event_types

    events = await app_client.get(f"/api/v1/tasks/{task['id']}/events", headers=headers)
    assert events.status_code == 200
    assert len(events.json()) >= 3


async def test_task_cancel(app_client: AsyncClient, owner: dict[str, str]) -> None:
    headers = {"Authorization": owner["Authorization"]}
    task = (
        await app_client.post(
            "/api/v1/tasks", headers=headers, json={"title": "t", "objective": "o"}
        )
    ).json()
    cancelled = await app_client.post(f"/api/v1/tasks/{task['id']}/cancel", headers=headers)
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"

    again = await app_client.post(f"/api/v1/tasks/{task['id']}/cancel", headers=headers)
    assert again.status_code == 409


async def test_task_listing_pagination(app_client: AsyncClient, owner: dict[str, str]) -> None:
    headers = {"Authorization": owner["Authorization"]}
    for index in range(3):
        await app_client.post(
            "/api/v1/tasks",
            headers=headers,
            json={"title": f"task {index}", "objective": "o"},
        )
    page = await app_client.get("/api/v1/tasks?limit=2&offset=0", headers=headers)
    assert page.status_code == 200
    assert page.json()["total"] == 3
    assert len(page.json()["items"]) == 2


async def test_viewer_cannot_create_tasks(app_client: AsyncClient, viewer: dict[str, str]) -> None:
    headers = {"Authorization": viewer["Authorization"]}
    response = await app_client.post(
        "/api/v1/tasks", headers=headers, json={"title": "t", "objective": "o"}
    )
    assert response.status_code == 403
