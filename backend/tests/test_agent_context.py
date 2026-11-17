"""Tests for per-run context assembly: skill catalogue, memory and tool resources."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.exceptions import NotFoundError
from app.db.enums import UserRole
from app.db.models import Agent, Task
from app.db.session import get_session_factory
from app.orchestrator.specialists import ORCHESTRATOR_SLUG, sync_specialists
from app.services import agent_context
from app.services import memory as memory_service
from app.services import skills as skill_service
from app.services.agents import sync_tool_registry
from app.services.users import create_user
from app.tools.builtin import BUILTIN_TOOLS

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
async def seeded() -> AsyncIterator[None]:
    factory = get_session_factory()
    async with factory() as session:
        await sync_tool_registry(session, [tool() for tool in BUILTIN_TOOLS])
        await sync_specialists(session)
        await skill_service.sync_builtin_skills(session)
        await session.commit()
    yield


async def _agent(session: Any, slug: str = ORCHESTRATOR_SLUG) -> Agent:
    return (await session.execute(select(Agent).where(Agent.slug == slug))).scalar_one()


async def _task(session: Any, agent: Agent, *, user_id: Any = None) -> Task:
    task = Task(
        title="Audit the payments contract",
        objective="Look for reentrancy bugs in the payment contract.",
        assigned_agent_id=agent.id,
        created_by=user_id,
    )
    session.add(task)
    await session.flush()
    return task


async def test_catalogue_lists_assigned_skills_only() -> None:
    factory = get_session_factory()
    async with factory() as session:
        agent = await _agent(session, "repo-analysis")
        catalogue = await agent_context.skill_catalogue_text(session, agent)
        assert catalogue is None or "## Available skills" not in catalogue

        skill = await skill_service.get_skill_by_slug(session, "code-review")
        await skill_service.assign_skill(session, agent, skill)
        await session.commit()

        catalogue = await agent_context.skill_catalogue_text(session, agent)
    assert "## Available skills" in catalogue
    assert "`code-review`" in catalogue
    assert "`security-audit`" not in catalogue
    assert "Call `load_skill`" in catalogue


async def test_extra_context_includes_memory_without_leaking_other_projects() -> None:
    factory = get_session_factory()
    async with factory() as session:
        agent = await _agent(session)
        task = await _task(session, agent)
        await memory_service.write_memory(
            session,
            memory_service.MemoryWrite(
                content="Reentrancy is the main risk in the payments contract.",
                layer=memory_service.MemoryLayer.SEMANTIC,
                source="test",
            ),
        )
        await session.commit()

        context = await agent_context.build_extra_context(session, agent=agent, task=task)
        await session.commit()

    assert "## Relevant memory" in context
    assert "Reentrancy" in context


async def test_memory_disabled_agent_gets_no_memory_section() -> None:
    factory = get_session_factory()
    async with factory() as session:
        agent = await _agent(session)
        agent.memory_config = {"enabled": False}
        task = await _task(session, agent)
        await memory_service.write_memory(
            session,
            memory_service.MemoryWrite(
                content="A durable fact that must not be injected.",
                layer=memory_service.MemoryLayer.SEMANTIC,
                source="test",
            ),
        )
        await session.commit()

        context = await agent_context.build_extra_context(session, agent=agent, task=task)

    assert "durable fact" not in context


async def test_memory_layer_restriction_is_applied() -> None:
    factory = get_session_factory()
    async with factory() as session:
        agent = await _agent(session)
        agent.memory_config = {"layers": ["project"]}
        task = await _task(session, agent)
        await memory_service.write_memory(
            session,
            memory_service.MemoryWrite(
                content="Semantic note about contract audits.",
                layer=memory_service.MemoryLayer.SEMANTIC,
                source="test",
            ),
        )
        await session.commit()

        context = await agent_context.build_extra_context(session, agent=agent, task=task)

    assert "Semantic note" not in context


async def test_tool_resources_load_remember_and_recall() -> None:
    factory = get_session_factory()
    async with factory() as session:
        agent = await _agent(session, "repo-analysis")
        skill = await skill_service.get_skill_by_slug(session, "code-review")
        await skill_service.assign_skill(session, agent, skill)
        task = await _task(session, agent)
        await session.commit()
        agent_id, task_id = agent.id, task.id

    resources = agent_context.tool_resources(
        factory, agent_id=agent_id, task_id=task_id, project_id=None, user_id=None
    )

    instructions = await resources["load_skill"]("code-review")
    assert "# Skill: Code Review" in instructions

    stored = await resources["remember"](
        {"content": "Payments contract uses a proxy pattern.", "layer": "semantic"}
    )
    assert stored["stored"] is True
    assert stored["version"] == 1

    results = await resources["recall"]("payments contract proxy", 5)
    assert results
    assert any("proxy pattern" in item["content"] for item in results)


async def test_tool_resources_reject_unassigned_skill() -> None:
    factory = get_session_factory()
    async with factory() as session:
        agent = await _agent(session, "report-writer")
        task = await _task(session, agent)
        await session.commit()
        agent_id, task_id = agent.id, task.id

    resources = agent_context.tool_resources(
        factory, agent_id=agent_id, task_id=task_id, project_id=None, user_id=None
    )
    with pytest.raises(NotFoundError):
        await resources["load_skill"]("code-review")


async def test_tool_resources_respect_disabled_memory() -> None:
    factory = get_session_factory()
    async with factory() as session:
        agent = await _agent(session)
        task = await _task(session, agent)
        await session.commit()
        agent_id, task_id = agent.id, task.id

    resources = agent_context.tool_resources(
        factory,
        agent_id=agent_id,
        task_id=task_id,
        project_id=None,
        user_id=None,
        memory_enabled=False,
    )
    with pytest.raises(RuntimeError, match="disabled"):
        await resources["remember"]({"content": "should not persist"})
    assert await resources["recall"]("anything", 5) == []


async def test_skill_load_is_recorded_in_execution_history() -> None:
    factory = get_session_factory()
    async with factory() as session:
        agent = await _agent(session, "repo-analysis")
        skill = await skill_service.get_skill_by_slug(session, "code-review")
        await skill_service.assign_skill(session, agent, skill)
        task = await _task(session, agent)
        await session.commit()
        skill_id = skill.id

    resources = agent_context.tool_resources(
        factory, agent_id=agent.id, task_id=task.id, project_id=None, user_id=None
    )
    await resources["load_skill"]("code-review")

    async with factory() as session:
        rows = await skill_service.execution_history(session, skill_id=skill_id)
    assert rows and rows[0].ok is True
    assert rows[0].event == "load"


async def test_successful_run_writes_episodic_memory(
    app_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decimal import Decimal

    from app.providers.base import LLMResponse, Usage

    class FakeProvider:
        def __init__(self) -> None:
            self.requests: list[Any] = []

        async def complete(self, request: Any) -> LLMResponse:
            self.requests.append(request)
            return LLMResponse(
                content="Contract is clean.",
                usage=Usage(prompt_tokens=5, completion_tokens=5, total_tokens=10),
                finish_reason="stop",
                model="fake-model",
            )

    factory = get_session_factory()
    async with factory() as session:
        await create_user(
            session,
            email="context-owner@example.com",
            password="Str0ng-Passw0rd!",
            role=UserRole.OWNER,
        )
        await session.commit()

    login = await app_client.post(
        "/api/v1/auth/login",
        json={"email": "context-owner@example.com", "password": "Str0ng-Passw0rd!"},
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    provider = (
        await app_client.post(
            "/api/v1/providers",
            headers=headers,
            json={
                "slug": "ctx-local",
                "name": "Ctx local",
                "provider_type": "local",
                "base_url": "http://127.0.0.1:9/v1",
                "default_model": "fake-model",
                "enabled": True,
            },
        )
    ).json()

    async with factory() as session:
        agent = await _agent(session)
        agent.model_provider_id = provider["id"]
        agent.model_name = "fake-model"
        agent_id = agent.id
        await session.commit()

    fake = FakeProvider()
    monkeypatch.setattr("app.services.execution.provider_from_record", lambda *a, **k: fake)

    task = (
        await app_client.post(
            "/api/v1/tasks",
            headers=headers,
            json={
                "title": "Check the contract",
                "objective": "Check the payments contract for reentrancy.",
                "assigned_agent_id": str(agent_id),
                "budget_usd": Decimal("1.00").__str__(),
            },
        )
    ).json()

    run = await app_client.post(f"/api/v1/tasks/{task['id']}/run", headers=headers)
    assert run.status_code == 200, run.text
    assert run.json()["status"] == "completed"

    memories = await app_client.get("/api/v1/memory", headers=headers, params={"layer": "episodic"})
    assert memories.status_code == 200, memories.text
    contents = [item["content"] for item in memories.json()]
    assert any("Check the contract" in content for content in contents), contents

    system_prompt = fake.requests[0].messages[0].content
    assert system_prompt.startswith("You are the Main Orchestrator")
    assert "## Available skills" not in system_prompt


async def test_resolve_workspace_returns_none_without_project() -> None:
    factory = get_session_factory()
    async with factory() as session:
        assert await agent_context.resolve_workspace(session, None) is None


async def test_resolve_workspace_opens_the_project_directory(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.config import get_settings

    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    get_settings.cache_clear()

    from app.services import workspaces as workspace_service

    factory = get_session_factory()
    async with factory() as session:
        project = await workspace_service.create_project(session, slug="ws-demo", name="WS Demo")
        await session.commit()
        workspace = await agent_context.resolve_workspace(session, project.id)

    assert workspace is not None
    assert workspace.root.name == "ws-demo"
    get_settings.cache_clear()


async def test_resolve_workspace_is_none_when_directory_is_gone(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    from app.core.config import get_settings

    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "gone-root"))
    get_settings.cache_clear()

    from app.services import workspaces as workspace_service

    factory = get_session_factory()
    async with factory() as session:
        project = await workspace_service.create_project(session, slug="ws-gone", name="WS Gone")
        await session.commit()
        shutil.rmtree(project.workspace_path, ignore_errors=True)
        workspace = await agent_context.resolve_workspace(session, project.id)

    assert workspace is None
    get_settings.cache_clear()
