"""Integration tests for orchestration, approvals and agent messaging."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import AsyncClient

from app.db.enums import UserRole
from app.providers.base import LLMRequest, LLMResponse, ToolCall, Usage
from app.services.users import create_user

pytestmark = pytest.mark.integration

PLAN_ARGS: dict[str, Any] = {
    "rationale": "Split the audit into scanning and reporting.",
    "steps": [
        {
            "key": "scan",
            "title": "Scan the repository",
            "instruction": "Scan the repository for risky files.",
            "agent_slug": "repo-analysis",
        },
        {
            "key": "report",
            "title": "Write the report",
            "instruction": "Write the audit report from the scan.",
            "agent_slug": "report-writer",
            "depends_on": ["scan"],
        },
    ],
}


class ScriptedProvider:
    """Plays back responses; records requests so tests can assert prompts."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("ScriptedProvider ran out of responses")
        return self.responses.pop(0)


def _text(content: str) -> LLMResponse:
    return LLMResponse(
        content=content,
        usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        finish_reason="stop",
        model="fake-model",
    )


def _plan_response(args: dict[str, Any] | None = None) -> LLMResponse:
    return LLMResponse(
        content="",
        tool_calls=[
            ToolCall(id="plan-1", name="submit_plan", arguments=args or PLAN_ARGS),
        ],
        usage=Usage(prompt_tokens=20, completion_tokens=10, total_tokens=30),
        finish_reason="tool_calls",
        model="fake-model",
    )


@pytest.fixture(autouse=True)
async def seeded() -> AsyncIterator[None]:
    """Mirror startup seeding: tools plus the built-in specialists."""
    from app.db.session import get_session_factory
    from app.orchestrator.specialists import sync_specialists
    from app.services.agents import sync_tool_registry
    from app.tools.builtin import BUILTIN_TOOLS

    factory = get_session_factory()
    async with factory() as session:
        await sync_tool_registry(session, [tool() for tool in BUILTIN_TOOLS])
        await sync_specialists(session)
        await session.commit()
    yield


async def _headers(client: AsyncClient, email: str) -> dict[str, str]:
    from app.db.session import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        await create_user(session, email=email, password="Str0ng-Passw0rd!", role=UserRole.OWNER)
        await session.commit()

    response = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "Str0ng-Passw0rd!"}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture
async def headers(app_client: AsyncClient) -> dict[str, str]:
    return await _headers(app_client, "orch-owner@example.com")


async def _provider(client: AsyncClient, headers: dict[str, str]) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/providers",
        headers=headers,
        json={
            "slug": "fake-orch",
            "name": "Fake orchestrator provider",
            "provider_type": "local",
            "base_url": "http://127.0.0.1:9/v1",
            "default_model": "fake-model",
            "enabled": True,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _assign_models(provider_id: str, model: str = "fake-model") -> None:
    """Give every seeded specialist the fake provider/model."""
    from sqlalchemy import select

    from app.db.models import Agent
    from app.db.session import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        for agent in (await session.execute(select(Agent))).scalars():
            agent.model_provider_id = provider_id
            agent.model_name = model
        await session.commit()


async def _task(client: AsyncClient, headers: dict[str, str], objective: str) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={"title": "Audit", "objective": objective},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _install_provider(monkeypatch: pytest.MonkeyPatch, provider: ScriptedProvider) -> None:
    """Replace provider construction so the orchestrator talks to the script."""
    import app.api.v1.orchestration as orchestration_api
    import app.providers.registry as registry
    import app.services.execution as execution
    import app.services.orchestration as orchestration

    fake = lambda record: provider  # noqa: E731 - a stub, not a style choice
    monkeypatch.setattr(registry, "provider_from_record", fake)
    monkeypatch.setattr(orchestration, "provider_from_record", fake)
    monkeypatch.setattr(execution, "provider_from_record", fake)
    monkeypatch.setattr(orchestration_api, "provider_from_record", fake)


async def test_plan_preview_uses_the_planner(
    app_client: AsyncClient, headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = await _provider(app_client, headers)
    await _assign_models(provider["id"])
    script = ScriptedProvider([_plan_response()])
    _install_provider(monkeypatch, script)

    response = await app_client.post(
        "/api/v1/orchestration/plan",
        headers=headers,
        json={"objective": "Audit the repository"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert [step["key"] for step in body["steps"]] == ["scan", "report"]
    assert body["steps"][1]["depends_on"] == ["scan"]
    assert body["rationale"].startswith("Split the audit")


async def test_plan_preview_requires_seeded_orchestrator(
    app_client: AsyncClient, headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy import delete

    from app.db.models import Agent
    from app.db.session import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        await session.execute(delete(Agent))
        await session.commit()

    response = await app_client.post(
        "/api/v1/orchestration/plan", headers=headers, json={"objective": "anything"}
    )
    assert response.status_code == 404


async def test_orchestration_delegates_steps_and_aggregates_output(
    app_client: AsyncClient, headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = await _provider(app_client, headers)
    await _assign_models(provider["id"])
    script = ScriptedProvider(
        [
            _plan_response(),
            _text("scan found two risky files"),
            _text("final report body"),
        ]
    )
    _install_provider(monkeypatch, script)

    task = await _task(app_client, headers, "Audit the repository")
    response = await app_client.post(
        f"/api/v1/orchestration/tasks/{task['id']}/run", headers=headers, json={}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "completed", body
    assert body["summary"]["steps_ok"] == 2
    assert {step["key"] for step in body["steps"]} == {"scan", "report"}
    assert "scan found two risky files" in body["content"]
    assert "final report body" in body["content"]
    assert body["totals"]["tokens"] > 0

    # The dependent step must have seen the earlier step's output.
    report_prompt = script.requests[-1]
    system_text = "\n".join(
        message.content for message in report_prompt.messages if message.role == "system"
    )
    assert "scan found two risky files" in system_text

    detail = await app_client.get(f"/api/v1/tasks/{task['id']}", headers=headers)
    assert detail.json()["status"] == "completed"

    children = await app_client.get(
        f"/api/v1/orchestration/tasks/{task['id']}/children", headers=headers
    )
    assert children.status_code == 200
    assert {child["plan_key"] for child in children.json()} == {"scan", "report"}
    assert all(child["status"] == "completed" for child in children.json())

    messages = await app_client.get(
        f"/api/v1/orchestration/tasks/{task['id']}/messages", headers=headers
    )
    assert messages.status_code == 200
    handoffs = [item for item in messages.json() if item["type"] == "handoff"]
    assert len(handoffs) == 2
    assert {item["from"] for item in handoffs} == {"repo-analysis", "report-writer"}


async def test_orchestration_honours_an_explicit_plan(
    app_client: AsyncClient, headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = await _provider(app_client, headers)
    await _assign_models(provider["id"])
    script = ScriptedProvider([_text("only step output")])
    _install_provider(monkeypatch, script)

    task = await _task(app_client, headers, "Do one thing")
    response = await app_client.post(
        f"/api/v1/orchestration/tasks/{task['id']}/run",
        headers=headers,
        json={
            "use_llm_planner": True,
            "plan": {
                "objective": "Do one thing",
                "steps": [
                    {
                        "key": "solo",
                        "title": "Solo",
                        "instruction": "Do the only step",
                        "agent_slug": "research",
                    }
                ],
            },
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "completed"
    # A supplied plan means the planner is never called: one model response only.
    assert len(script.requests) == 1


async def test_orchestration_fails_when_a_step_fails(
    app_client: AsyncClient, headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = await _provider(app_client, headers)
    await _assign_models(provider["id"])
    script = ScriptedProvider([_plan_response()])
    _install_provider(monkeypatch, script)

    task = await _task(app_client, headers, "Audit the repository")
    response = await app_client.post(
        f"/api/v1/orchestration/tasks/{task['id']}/run", headers=headers, json={}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "failed"
    assert "scan" in (body["error"] or "")

    children = await app_client.get(
        f"/api/v1/orchestration/tasks/{task['id']}/children", headers=headers
    )
    statuses = {child["plan_key"]: child["status"] for child in children.json()}
    assert statuses["scan"] == "failed"
    # The dependent step is skipped, and its child task is cancelled rather than
    # left dangling in `pending`.
    assert statuses["report"] == "cancelled"


async def test_orchestration_retries_a_flaky_step(
    app_client: AsyncClient, headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = await _provider(app_client, headers)
    await _assign_models(provider["id"])
    script = ScriptedProvider(
        [
            _plan_response(
                {
                    "steps": [
                        {
                            "key": "solo",
                            "title": "Solo",
                            "instruction": "Do it",
                            "agent_slug": "research",
                        }
                    ]
                }
            ),
            _text("first attempt result"),
            _text("retry result"),
        ]
    )
    _install_provider(monkeypatch, script)

    task = await _task(app_client, headers, "Try twice")
    response = await app_client.post(
        f"/api/v1/orchestration/tasks/{task['id']}/run",
        headers=headers,
        json={"max_step_attempts": 2},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    # The scripted run succeeds because the step itself succeeds; retries are
    # exercised in the scheduler unit tests. This asserts the wiring only.
    assert body["status"] == "completed"


async def test_unknown_agent_in_explicit_plan_is_rejected(
    app_client: AsyncClient, headers: dict[str, str]
) -> None:
    task = await _task(app_client, headers, "Do the impossible")
    response = await app_client.post(
        f"/api/v1/orchestration/tasks/{task['id']}/run",
        headers=headers,
        json={
            "plan": {
                "objective": "Do the impossible",
                "steps": [
                    {
                        "key": "ghost",
                        "title": "Ghost",
                        "instruction": "x",
                        "agent_slug": "does-not-exist",
                    }
                ],
            }
        },
    )
    assert response.status_code == 422, response.text
    assert "does not exist" in response.text


async def test_cyclic_explicit_plan_is_rejected(
    app_client: AsyncClient, headers: dict[str, str]
) -> None:
    task = await _task(app_client, headers, "Cycle")
    response = await app_client.post(
        f"/api/v1/orchestration/tasks/{task['id']}/run",
        headers=headers,
        json={
            "plan": {
                "objective": "Cycle",
                "steps": [
                    {
                        "key": "a",
                        "title": "A",
                        "instruction": "a",
                        "agent_slug": "research",
                        "depends_on": ["b"],
                    },
                    {
                        "key": "b",
                        "title": "B",
                        "instruction": "b",
                        "agent_slug": "research",
                        "depends_on": ["a"],
                    },
                ],
            }
        },
    )
    assert response.status_code == 422
    assert "cycle" in response.text


async def test_orchestration_is_idempotent_against_a_completed_task(
    app_client: AsyncClient, headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = await _provider(app_client, headers)
    await _assign_models(provider["id"])
    script = ScriptedProvider(
        [
            _plan_response(
                {
                    "steps": [
                        {
                            "key": "solo",
                            "title": "Solo",
                            "instruction": "Do it",
                            "agent_slug": "research",
                        }
                    ]
                }
            ),
            _text("done"),
        ]
    )
    _install_provider(monkeypatch, script)

    task = await _task(app_client, headers, "Once")
    first = await app_client.post(
        f"/api/v1/orchestration/tasks/{task['id']}/run", headers=headers, json={}
    )
    assert first.json()["status"] == "completed"

    second = await app_client.post(
        f"/api/v1/orchestration/tasks/{task['id']}/run", headers=headers, json={}
    )
    assert second.status_code == 409


async def test_approvals_crud_and_decisions(
    app_client: AsyncClient, headers: dict[str, str]
) -> None:
    created = await app_client.post(
        "/api/v1/approvals",
        headers=headers,
        json={
            "title": "Submit finding",
            "description": "Publish the report externally",
            "action_type": "submit_competition",
        },
    )
    assert created.status_code == 201, created.text
    approval = created.json()
    assert approval["status"] == "pending"

    listed = await app_client.get("/api/v1/approvals", headers=headers)
    assert listed.status_code == 200
    assert listed.json()["total"] == 1

    decided = await app_client.post(
        f"/api/v1/approvals/{approval['id']}/decide",
        headers=headers,
        json={"approved": True, "note": "looks good"},
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["status"] == "approved"
    assert decided.json()["decision_note"] == "looks good"

    again = await app_client.post(
        f"/api/v1/approvals/{approval['id']}/decide",
        headers=headers,
        json={"approved": False},
    )
    assert again.status_code == 409


async def test_approval_decision_writes_a_task_event(
    app_client: AsyncClient, headers: dict[str, str]
) -> None:
    task = await _task(app_client, headers, "Needs a decision")
    created = await app_client.post(
        "/api/v1/approvals",
        headers=headers,
        json={"title": "Spend budget", "task_id": task["id"], "action_type": "spend_budget"},
    )
    assert created.status_code == 201, created.text

    decided = await app_client.post(
        f"/api/v1/approvals/{created.json()['id']}/decide",
        headers=headers,
        json={"approved": False, "note": "too expensive"},
    )
    assert decided.status_code == 200

    events = await app_client.get(f"/api/v1/tasks/{task['id']}/events", headers=headers)
    types = [event["type"] for event in events.json()]
    assert "approval.decided" in types


async def test_approval_requires_operator_role(
    app_client: AsyncClient, headers: dict[str, str]
) -> None:
    viewer_headers = await _headers(app_client, "orch-viewer@example.com")
    from sqlalchemy import update

    from app.db.models import User
    from app.db.session import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        await session.execute(
            update(User).where(User.email == "orch-viewer@example.com").values(role=UserRole.VIEWER)
        )
        await session.commit()

    response = await app_client.get("/api/v1/approvals", headers=viewer_headers)
    assert response.status_code == 403


async def test_resume_requires_a_persisted_plan(
    app_client: AsyncClient, headers: dict[str, str]
) -> None:
    task = await _task(app_client, headers, "Nothing planned")
    response = await app_client.post(
        f"/api/v1/orchestration/tasks/{task['id']}/resume", headers=headers, json={}
    )
    assert response.status_code == 409


async def test_sync_specialists_overwrite_repoints_the_model() -> None:

    from sqlalchemy import select

    from app.db.models import Agent, ModelProvider
    from app.db.session import get_session_factory
    from app.orchestrator.specialists import sync_specialists

    factory = get_session_factory()
    async with factory() as session:
        first = ModelProvider(
            slug="sync-first",
            name="First",
            provider_type="openai_compatible",
            base_url="http://127.0.0.1:9/v1",
            default_model="model-a",
            enabled=True,
        )
        second = ModelProvider(
            slug="sync-second",
            name="Second",
            provider_type="openai_compatible",
            base_url="http://127.0.0.1:9/v1",
            default_model="model-b",
            enabled=True,
        )
        session.add_all([first, second])
        await session.flush()

        await sync_specialists(session, default_provider_id=first.id, default_model="model-a")
        await session.commit()

        report = await sync_specialists(
            session, default_provider_id=second.id, default_model="model-b", overwrite=True
        )
        await session.commit()
        assert len(report.updated) == 15

        agent = (
            await session.execute(select(Agent).where(Agent.slug == "main-orchestrator"))
        ).scalar_one()
        assert agent.model_provider_id == second.id
        assert agent.model_name == "model-b"

        # Without overwrite, an operator's manual choice is preserved.
        await sync_specialists(session, default_provider_id=first.id, default_model="model-a")
        await session.commit()
        await session.refresh(agent)
        assert agent.model_provider_id == second.id


async def test_sync_specialists_is_idempotent() -> None:
    from sqlalchemy import func, select

    from app.db.models import Agent
    from app.db.session import get_session_factory
    from app.orchestrator.specialists import sync_specialists

    factory = get_session_factory()
    async with factory() as session:
        report = await sync_specialists(session)
        await session.commit()
        assert report.created == []
        assert len(report.unchanged) == 15
        count = (await session.execute(select(func.count()).select_from(Agent))).scalar_one()
        assert count == 15
