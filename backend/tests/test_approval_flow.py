"""Integration tests for the human-approval round trip.

An agent may only run a high-risk tool (writing files, running commands) once a
human has approved that specific request. These tests drive the whole path
against the real database and API: the runtime refuses the call, execution
records an ``approvals`` row and parks the task in ``waiting_approval``,
a decision flips it, and a re-run then really executes the tool.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.db.enums import ApprovalStatus, RiskLevel, TaskStatus, UserRole
from app.db.models import Approval
from app.db.session import get_session_factory
from app.providers.base import LLMRequest, LLMResponse, ToolCall, Usage
from app.services.users import create_user
from app.tools.base import Tool, ToolContext, ToolRegistry

pytestmark = pytest.mark.integration


class ScriptedProvider:
    def __init__(self) -> None:
        self.queue: list[LLMResponse] = []
        self.requests: list[LLMRequest] = []

    def push(self, *responses: LLMResponse) -> None:
        self.queue.extend(responses)

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if not self.queue:
            raise AssertionError("ScriptedProvider ran out of responses")
        return self.queue.pop(0)


class WritingTool(Tool):
    """Stands in for a real write tool: high risk, needs approval."""

    slug = "write_probe"
    name = "Write probe"
    description = "writes a probe file"
    parameters = {"type": "object", "properties": {"path": {"type": "string"}}}
    required_permissions = ("filesystem:write",)
    risk_level = RiskLevel.HIGH

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        self.calls.append(arguments)
        return {"written": arguments.get("path")}


def _tool_call_response() -> LLMResponse:
    return LLMResponse(
        content="",
        tool_calls=[ToolCall(id="c1", name="write_probe", arguments={"path": "note.txt"})],
        usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        finish_reason="tool_calls",
        model="fake-model",
    )


def _text_response(text: str) -> LLMResponse:
    return LLMResponse(
        content=text,
        usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        finish_reason="stop",
        model="fake-model",
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
async def headers(app_client: AsyncClient) -> dict[str, str]:
    factory = get_session_factory()
    async with factory() as session:
        await create_user(
            session,
            email="approval-owner@example.com",
            password="Str0ng-Passw0rd!",
            role=UserRole.OWNER,
        )
        await session.commit()

    response = await app_client.post(
        "/api/v1/auth/login",
        json={"email": "approval-owner@example.com", "password": "Str0ng-Passw0rd!"},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _prepare_agent() -> dict[str, Any]:
    """Point one specialist at the scripted provider and give it the probe tool."""
    from app.db.models import Agent, AgentTool
    from app.db.models import ToolRegistry as ToolRow

    factory = get_session_factory()
    async with factory() as session:
        agent = (
            await session.execute(select(Agent).where(Agent.slug == "software-developer"))
        ).scalar_one()
        provider = await _fake_provider_row()
        agent.model_provider_id = provider
        agent.model_name = "fake-model"
        agent.permissions = {"tools": ["filesystem:write", "tool:read"]}

        for slug in ("write_probe",):
            tool_row = (
                await session.execute(select(ToolRow).where(ToolRow.slug == slug))
            ).scalar_one_or_none()
            if tool_row is None:
                tool_row = ToolRow(
                    slug=slug,
                    name="Write probe",
                    description="writes a probe file",
                    input_schema={"type": "object", "properties": {}},
                    required_permissions=["filesystem:write"],
                    risk_level=RiskLevel.HIGH,
                    timeout_seconds=30,
                    execution_environment="sandbox",
                    enabled=True,
                )
                session.add(tool_row)
                await session.flush()
            session.add(AgentTool(agent_id=agent.id, tool_id=tool_row.id, enabled=True, config={}))
        await session.commit()
        return {"agent_id": str(agent.id)}


async def _fake_provider_row() -> Any:
    from app.db.models import ModelProvider

    factory = get_session_factory()
    async with factory() as session:
        provider = ModelProvider(
            slug="fake-approval",
            name="Fake approval provider",
            provider_type="local",
            base_url="http://127.0.0.1:9/v1",
            default_model="fake-model",
            enabled=True,
        )
        session.add(provider)
        await session.commit()
        await session.refresh(provider)
        return provider.id


async def _create_task(client: AsyncClient, headers: dict[str, str], agent_id: str) -> dict:
    response = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "title": "Write a note",
            "objective": "Write note.txt in the workspace.",
            "assigned_agent_id": agent_id,
            "max_iterations": 4,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _install(monkeypatch: pytest.MonkeyPatch, provider: ScriptedProvider) -> WritingTool:
    import app.api.v1.tasks as tasks_api
    import app.providers.registry as provider_registry
    import app.services.execution as execution
    from app.tools.builtin import default_tool_registry

    tool = WritingTool()

    monkeypatch.setattr(provider_registry, "provider_from_record", lambda record: provider)
    monkeypatch.setattr(execution, "provider_from_record", lambda record: provider)

    def registry() -> ToolRegistry:
        base = default_tool_registry()
        base.register(tool)
        return base

    monkeypatch.setattr(tasks_api, "default_tool_registry", registry)
    return tool


async def test_high_risk_tool_is_recorded_and_the_task_waits(
    app_client: AsyncClient, headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = await _prepare_agent()
    task = await _create_task(app_client, headers, agent["agent_id"])
    provider = ScriptedProvider()
    provider.push(_tool_call_response(), _text_response("I could not write the file."))
    tool = _install(monkeypatch, provider)

    response = await app_client.post(f"/api/v1/tasks/{task['id']}/run", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["status"] == TaskStatus.WAITING_APPROVAL.value
    assert body["pending_approvals"][0]["tool"] == "write_probe"
    assert body["pending_approvals"][0]["risk_level"] == "high"
    assert tool.calls == []

    factory = get_session_factory()
    async with factory() as session:
        rows = (
            (await session.execute(select(Approval).where(Approval.task_id == task["id"])))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].status == ApprovalStatus.PENDING
    assert rows[0].payload["tool"] == "write_probe"
    assert rows[0].payload["arguments"] == {"path": "note.txt"}


async def test_approved_tool_runs_on_the_next_run(
    app_client: AsyncClient, headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = await _prepare_agent()
    task = await _create_task(app_client, headers, agent["agent_id"])
    provider = ScriptedProvider()
    provider.push(_tool_call_response(), _text_response("waiting"))
    tool = _install(monkeypatch, provider)

    first = await app_client.post(f"/api/v1/tasks/{task['id']}/run", headers=headers)
    assert first.json()["status"] == TaskStatus.WAITING_APPROVAL.value

    listing = await app_client.get("/api/v1/approvals", headers=headers)
    assert listing.status_code == 200, listing.text
    approval = next(item for item in listing.json()["items"] if item["task_id"] == task["id"])

    decision = await app_client.post(
        f"/api/v1/approvals/{approval['id']}/decide",
        headers=headers,
        json={"approved": True, "note": "writing a probe file is fine"},
    )
    assert decision.status_code == 200, decision.text
    assert decision.json()["status"] == ApprovalStatus.APPROVED.value

    provider.push(_tool_call_response(), _text_response("Wrote note.txt."))
    second = await app_client.post(f"/api/v1/tasks/{task['id']}/run", headers=headers)
    assert second.status_code == 200, second.text
    body = second.json()

    assert body["status"] == TaskStatus.COMPLETED.value
    assert body["pending_approvals"] == []
    assert tool.calls == [{"path": "note.txt"}]
    assert "note.txt" in body["content"]


async def test_rejected_approval_leaves_the_tool_blocked(
    app_client: AsyncClient, headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = await _prepare_agent()
    task = await _create_task(app_client, headers, agent["agent_id"])
    provider = ScriptedProvider()
    provider.push(_tool_call_response(), _text_response("waiting"))
    tool = _install(monkeypatch, provider)

    await app_client.post(f"/api/v1/tasks/{task['id']}/run", headers=headers)
    listing = await app_client.get("/api/v1/approvals", headers=headers)
    approval = next(item for item in listing.json()["items"] if item["task_id"] == task["id"])

    await app_client.post(
        f"/api/v1/approvals/{approval['id']}/decide",
        headers=headers,
        json={"approved": False, "note": "no"},
    )

    provider.push(_tool_call_response(), _text_response("still blocked"))
    second = await app_client.post(f"/api/v1/tasks/{task['id']}/run", headers=headers)
    assert second.status_code == 200, second.text

    assert tool.calls == []
    assert second.json()["status"] == TaskStatus.WAITING_APPROVAL.value


async def test_duplicate_pending_requests_are_not_created(
    app_client: AsyncClient, headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = await _prepare_agent()
    task = await _create_task(app_client, headers, agent["agent_id"])
    provider = ScriptedProvider()
    provider.push(_tool_call_response(), _text_response("waiting"))
    _install(monkeypatch, provider)

    await app_client.post(f"/api/v1/tasks/{task['id']}/run", headers=headers)

    provider.push(_tool_call_response(), _text_response("waiting again"))
    await app_client.post(f"/api/v1/tasks/{task['id']}/run", headers=headers)

    factory = get_session_factory()
    async with factory() as session:
        rows = (
            (await session.execute(select(Approval).where(Approval.task_id == task["id"])))
            .scalars()
            .all()
        )
    assert len(rows) == 1
