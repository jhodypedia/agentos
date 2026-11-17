"""End-to-end journey through the public HTTP API.

Every other test module checks one layer. This one walks the path a real operator
takes — log in, create an agent, create a project, create a task, watch it run,
read the dashboard, find the run in the logs — and asserts the pieces agree with
each other. It is intentionally a small number of large tests: its job is to
catch wiring mistakes that unit tests, being scoped to one module, cannot.

A task is executed inline here (via ``/tasks/{id}/run``) rather than through the
worker, so the suite does not depend on a background process; ``test_worker.py``
covers the worker path separately.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import AsyncClient

from app.db.enums import UserRole
from app.db.session import get_session_factory
from app.providers.base import LLMRequest, LLMResponse, Usage
from app.services.users import create_user

pytestmark = pytest.mark.integration


class ScriptedProvider:
    """Answers every request with the next canned response."""

    def __init__(self, *responses: LLMResponse) -> None:
        self.queue = list(responses)
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if not self.queue:
            # A model that keeps being called means the loop is not terminating.
            raise AssertionError("ScriptedProvider ran out of responses")
        return self.queue.pop(0)

    async def aclose(self) -> None:
        return None


def _reply(text: str, *, tokens: int = 24) -> LLMResponse:
    return LLMResponse(
        content=text,
        usage=Usage(prompt_tokens=tokens, completion_tokens=8, total_tokens=tokens + 8),
        finish_reason="stop",
        model="e2e-model",
    )


@pytest.fixture(autouse=True)
async def seeded() -> AsyncIterator[None]:
    from app.orchestrator.specialists import sync_specialists
    from app.services.agents import sync_tool_registry
    from app.tools.builtin import BUILTIN_TOOLS

    factory = get_session_factory()
    async with factory() as session:
        await sync_tool_registry(session, [tool() for tool in BUILTIN_TOOLS])
        await sync_specialists(session)
        await session.commit()
    yield


@pytest.fixture
async def owner(app_client: AsyncClient) -> dict[str, str]:
    factory = get_session_factory()
    async with factory() as session:
        await create_user(
            session,
            email="e2e-owner@example.com",
            password="Str0ng-Passw0rd!",
            role=UserRole.OWNER,
        )
        await session.commit()
    response = await app_client.post(
        "/api/v1/auth/login",
        json={"email": "e2e-owner@example.com", "password": "Str0ng-Passw0rd!"},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def test_full_operator_journey(
    app_client: AsyncClient, owner: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1. Identity: the token works and reports the role.
    me = await app_client.get("/api/v1/auth/me", headers=owner)
    assert me.status_code == 200
    assert me.json()["role"] == "owner"

    # 2. A project to hold the work.
    project = await app_client.post(
        "/api/v1/projects",
        headers=owner,
        json={
            "slug": "e2e-project",
            "name": "E2E project",
            "description": "created by the end-to-end test",
        },
    )
    assert project.status_code in (200, 201), project.text
    project_id = project.json()["id"]

    # 3. An agent pointed at a scripted provider.
    provider = ScriptedProvider(_reply("E2E done."))
    from app.services import execution as execution_service

    async def resolver(db: object, agent: object) -> object:
        return provider

    monkeypatch.setattr(execution_service, "_resolve_provider", resolver)

    from sqlalchemy import select

    from app.db.models import Agent

    factory = get_session_factory()
    async with factory() as db:
        agent_row = await db.scalar(select(Agent).where(Agent.slug == "report-writer").limit(1))
        assert agent_row is not None
        agent_id = str(agent_row.id)

    # The seeded specialist has no model, so give it one. This goes through the
    # API rather than the database, which also exercises the admin-only update
    # path a real operator would use.
    patch = await app_client.patch(
        f"/api/v1/agents/{agent_id}",
        headers=owner,
        json={"model_name": "e2e-model"},
    )
    assert patch.status_code == 200, patch.text

    # 4. A task assigned to that agent.
    created = await app_client.post(
        "/api/v1/tasks",
        headers=owner,
        json={
            "title": "E2E task",
            "objective": "Produce the end-to-end marker",
            "assigned_agent_id": agent_id,
            "project_id": project_id,
            "max_iterations": 1,
        },
    )
    assert created.status_code == 201, created.text
    task_id = created.json()["id"]
    assert created.json()["status"] == "pending"

    # 5. Run it inline and check the outcome.
    run = await app_client.post(f"/api/v1/tasks/{task_id}/run", headers=owner)
    assert run.status_code == 200, run.text
    body = run.json()
    assert body["status"] == "completed", body.get("error")
    assert body["content"] == "E2E done."
    assert body["total_tokens"] == 32
    assert provider.requests, "the runtime never reached the provider"

    # 6. The run is visible as history, not just as a response.
    detail = await app_client.get(f"/api/v1/tasks/{task_id}", headers=owner)
    assert detail.status_code == 200
    events = [event["event_type"] for event in detail.json()["events"]]
    assert "task.created" in events
    assert "task.completed" in events

    # 7. The dashboard counts it.
    dashboard = await app_client.get("/api/v1/dashboard/summary", headers=owner)
    assert dashboard.status_code == 200
    summary = dashboard.json()
    assert summary["tasks_by_status"]["completed"] >= 1

    # 8. The run is searchable in the ops log.
    logs = await app_client.get(
        "/api/v1/ops/logs", headers=owner, params={"task_id": task_id, "limit": 50}
    )
    assert logs.status_code == 200
    assert logs.json()["total"] >= 1

    # 9. Usage is attributed, so cost is auditable.
    usage = await app_client.get("/api/v1/dashboard/usage", headers=owner, params={"days": 1})
    assert usage.status_code == 200
    points = usage.json()["points"]
    assert points, "the run should appear in the usage series"
    assert points[-1]["tokens"] >= 32


async def test_a_viewer_sees_the_dashboard_but_not_the_work(
    app_client: AsyncClient, owner: dict[str, str]
) -> None:
    """The permission boundary is part of the journey, not an afterthought.

    A viewer is a read-only observer: the dashboard aggregates are theirs, the
    task and agent surfaces (which carry objectives, prompts and outputs) are
    not. That asymmetry is deliberate, so it is asserted rather than assumed.
    """
    factory = get_session_factory()
    async with factory() as session:
        await create_user(
            session,
            email="e2e-viewer@example.com",
            password="Str0ng-Passw0rd!",
            role=UserRole.VIEWER,
        )
        await session.commit()

    login = await app_client.post(
        "/api/v1/auth/login",
        json={"email": "e2e-viewer@example.com", "password": "Str0ng-Passw0rd!"},
    )
    viewer = {"Authorization": f"Bearer {login.json()['access_token']}"}

    # Aggregates are readable.
    assert (await app_client.get("/api/v1/dashboard/summary", headers=viewer)).status_code == 200
    assert (await app_client.get("/api/v1/dashboard/usage", headers=viewer)).status_code == 200
    # Task detail and creation are not.
    assert (await app_client.get("/api/v1/tasks", headers=viewer)).status_code == 403
    denied = await app_client.post(
        "/api/v1/tasks",
        headers=viewer,
        json={"title": "nope", "objective": "should be refused"},
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "permission_denied"


async def test_health_and_metrics_are_reachable_without_credentials(
    app_client: AsyncClient,
) -> None:
    """A load balancer and a scraper have no token; both endpoints must answer."""
    live = await app_client.get("/health/live")
    assert live.status_code == 200
    ready = await app_client.get("/health/ready")
    assert ready.status_code == 200
    metrics = await app_client.get("/metrics")
    assert metrics.status_code == 200
    assert "agentos_http_requests_total" in metrics.text


async def test_unauthenticated_requests_are_rejected_everywhere(app_client: AsyncClient) -> None:
    for path in (
        "/api/v1/tasks",
        "/api/v1/agents",
        "/api/v1/ops/status",
        "/api/v1/dashboard/summary",
    ):
        response = await app_client.get(path)
        assert response.status_code == 401, path


async def test_error_shape_is_stable(app_client: AsyncClient, owner: dict[str, str]) -> None:
    """Clients parse this shape; it must not drift."""
    missing = await app_client.get(
        "/api/v1/tasks/00000000-0000-0000-0000-000000000000", headers=owner
    )
    assert missing.status_code == 404
    error = missing.json()["error"]
    assert set(error) >= {"code", "message", "details", "request_id"}
    assert error["request_id"]

    invalid = await app_client.post(
        "/api/v1/tasks", headers=owner, json={"title": "", "objective": ""}
    )
    assert invalid.status_code == 422


async def test_request_id_is_echoed(app_client: AsyncClient) -> None:
    response = await app_client.get("/health/live", headers={"X-Request-ID": "e2e-correlation"})
    assert response.headers["X-Request-ID"] == "e2e-correlation"
