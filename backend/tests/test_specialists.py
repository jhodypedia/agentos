"""Tests for specialist seeding: defaults, overrides and additive upgrades."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import select

from app.db.models import Agent, AgentTool, ToolRegistry
from app.db.session import get_session_factory
from app.orchestrator.specialists import (
    CODING_PERMISSIONS,
    CODING_TOOL_SLUGS,
    MEMORY_TOOL_SLUGS,
    MEMORY_WRITE_PERMISSION,
    ORCHESTRATOR_SLUG,
    READONLY_CODING_TOOL_SLUGS,
    SPECIALISTS,
    merged_definitions,
    sync_specialists,
)
from app.services.agents import sync_tool_registry
from app.tools.builtin import BUILTIN_TOOLS

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
async def seeded_tools() -> AsyncIterator[None]:
    factory = get_session_factory()
    async with factory() as session:
        await sync_tool_registry(session, [tool() for tool in BUILTIN_TOOLS])
        await session.commit()
    yield


async def test_every_specialist_can_use_skills_and_memory() -> None:
    for definition in SPECIALISTS:
        assert set(MEMORY_TOOL_SLUGS) <= set(definition.tools), definition.slug
        assert MEMORY_WRITE_PERMISSION in definition.permissions, definition.slug


async def test_coding_specialists_get_workspace_tools() -> None:
    definitions = {item.slug: item for item in SPECIALISTS}

    developer = definitions["software-developer"]
    assert set(CODING_TOOL_SLUGS) <= set(developer.tools)
    for permission in CODING_PERMISSIONS:
        assert permission in developer.permissions

    for slug in (
        "repo-analysis",
        "smart-contract-auditor",
        "report-writer",
        "independent-reviewer",
    ):
        assert set(READONLY_CODING_TOOL_SLUGS) <= set(definitions[slug].tools), slug
        assert "write_file" not in definitions[slug].tools, slug
        assert "run_command" not in definitions[slug].tools, slug
        assert "filesystem:read" in definitions[slug].permissions, slug

    for slug in ("testing", "optimization"):
        assert "run_command" in definitions[slug].tools, slug
        assert "write_file" not in definitions[slug].tools, slug
        assert "tools:execute" in definitions[slug].permissions, slug


async def test_specialists_without_workspace_identity_get_no_coding_tools() -> None:
    definitions = {item.slug: item for item in SPECIALISTS}
    for slug in ("bounty-discovery", "make-fun", "compliance", "vuln-validation"):
        assert not set(CODING_TOOL_SLUGS) & set(definitions[slug].tools), slug


async def test_orchestrator_is_flagged() -> None:
    orchestrator = next(item for item in SPECIALISTS if item.slug == ORCHESTRATOR_SLUG)
    assert orchestrator.is_orchestrator
    assert not any(item.is_orchestrator for item in SPECIALISTS if item is not orchestrator)


async def test_sync_assigns_memory_tools_and_grants_permission() -> None:
    factory = get_session_factory()
    async with factory() as session:
        report = await sync_specialists(session)
        await session.commit()

    assert ORCHESTRATOR_SLUG in report.created
    assert not report.skipped_tools

    async with factory() as session:
        agent = (
            await session.execute(select(Agent).where(Agent.slug == ORCHESTRATOR_SLUG))
        ).scalar_one()
        permissions = set((agent.permissions or {}).get("tools") or [])
        assert MEMORY_WRITE_PERMISSION in permissions

        slugs = {
            str(row)
            for row in (
                await session.execute(
                    select(ToolRegistry.slug)
                    .join(AgentTool, AgentTool.tool_id == ToolRegistry.id)
                    .where(AgentTool.agent_id == agent.id)
                )
            ).scalars()
        }
        assert set(MEMORY_TOOL_SLUGS) <= slugs


async def test_sync_is_idempotent() -> None:
    factory = get_session_factory()
    async with factory() as session:
        await sync_specialists(session)
        await session.commit()

    async with factory() as session:
        report = await sync_specialists(session)
        await session.commit()
    assert report.created == []
    assert len(report.unchanged) == len(SPECIALISTS)
    assert report.granted_permissions == {}


async def test_sync_preserves_operator_grants_and_edits() -> None:
    factory = get_session_factory()
    async with factory() as session:
        await sync_specialists(session)
        await session.commit()

    async with factory() as session:
        agent = (
            await session.execute(select(Agent).where(Agent.slug == ORCHESTRATOR_SLUG))
        ).scalar_one()
        agent.permissions = {"tools": ["tool:read", "shell:exec"]}
        agent.system_prompt = "Operator edited prompt."
        await session.commit()

    async with factory() as session:
        report = await sync_specialists(session)
        await session.commit()

    # Only the permission the operator was missing is granted; the existing
    # grant survives because the merge is additive.
    assert report.granted_permissions[ORCHESTRATOR_SLUG] == [MEMORY_WRITE_PERMISSION]

    async with factory() as session:
        agent = (
            await session.execute(select(Agent).where(Agent.slug == ORCHESTRATOR_SLUG))
        ).scalar_one()
        permissions = set((agent.permissions or {}).get("tools") or [])
        assert "shell:exec" in permissions
        assert "tool:read" in permissions
        assert MEMORY_WRITE_PERMISSION in permissions
        assert agent.system_prompt == "Operator edited prompt."


async def test_overrides_can_remove_tools_but_memory_is_re_added() -> None:
    definitions = merged_definitions(
        {"repo-analysis": {"tools": ["echo"], "permissions": ["tool:read"]}}
    )
    definition = next(item for item in definitions if item.slug == "repo-analysis")
    assert "echo" in definition.tools
    assert set(MEMORY_TOOL_SLUGS) <= set(definition.tools)
    assert MEMORY_WRITE_PERMISSION in definition.permissions


async def test_unknown_override_slug_is_rejected() -> None:
    from app.core.exceptions import UnprocessableError

    with pytest.raises(UnprocessableError):
        merged_definitions({"not-an-agent": {}})


def test_definitions_are_serialisable() -> None:
    payload: dict[str, Any] = merged_definitions()[0].to_dict()
    assert payload["slug"] == ORCHESTRATOR_SLUG
    assert isinstance(payload["tools"], list)
    assert payload["metadata"]["builtin"] is True
