"""Tests for the skill/memory tools and the runtime resource plumbing."""

from __future__ import annotations

from typing import Any

import pytest

from app.db.enums import RiskLevel
from app.runtime.types import AgentSpec
from app.tools import default_tool_registry
from app.tools.base import ToolContext
from app.tools.errors import (
    ToolExecutionError,
    ToolPermissionError,
    ToolValidationError,
)
from app.tools.memory import LoadSkillTool, MemorySearchTool, MemoryWriteTool


def _context(**resources: Any) -> ToolContext:
    return ToolContext(
        permissions=frozenset({"tool:read", "memory:write"}), resources=dict(resources)
    )


async def test_load_skill_calls_the_injected_loader() -> None:
    calls: list[str] = []

    async def loader(slug: str) -> str:
        calls.append(slug)
        return "# Skill: Code Review\nreview the diff"

    result = await LoadSkillTool().run({"slug": "code-review"}, _context(load_skill=loader))
    assert result.ok
    assert calls == ["code-review"]
    assert result.output["slug"] == "code-review"
    assert "review the diff" in result.output["instructions"]


async def test_load_skill_without_resource_fails() -> None:
    result = await LoadSkillTool().run({"slug": "code-review"}, _context())
    assert not result.ok
    assert "not available" in (result.error or "")


async def test_load_skill_reports_loader_errors() -> None:
    async def loader(slug: str) -> str:
        raise RuntimeError("Skill 'nope' is not assigned to this agent")

    result = await LoadSkillTool().run({"slug": "nope"}, _context(load_skill=loader))
    assert not result.ok
    assert "not assigned" in (result.error or "")


async def test_load_skill_validates_arguments() -> None:
    async def loader(slug: str) -> str:
        raise AssertionError("must not be called with invalid arguments")

    with pytest.raises(ToolValidationError):
        await LoadSkillTool().run({}, _context(load_skill=loader))


async def test_remember_forwards_a_typed_payload() -> None:
    seen: list[dict[str, Any]] = []

    async def writer(payload: dict[str, Any]) -> dict[str, Any]:
        seen.append(payload)
        return {"id": "m1", "layer": payload["layer"], "version": 1, "stored": True}

    result = await MemoryWriteTool().run(
        {"content": "pgvector needs a dimension.", "layer": "project", "importance": 0.9},
        _context(remember=writer),
    )
    assert result.ok
    assert result.output["stored"] is True
    assert seen[0]["content"] == "pgvector needs a dimension."
    assert seen[0]["layer"] == "project"
    assert seen[0]["importance"] == pytest.approx(0.9)
    assert seen[0]["key"] is None


async def test_remember_requires_memory_write_permission() -> None:
    async def writer(payload: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("must not be called without permission")

    with pytest.raises(ToolPermissionError, match="memory:write"):
        await MemoryWriteTool().run(
            {"content": "hello"},
            ToolContext(permissions=frozenset({"tool:read"}), resources={"remember": writer}),
        )


async def test_remember_is_medium_risk_and_not_auto_approved() -> None:
    tool = MemoryWriteTool()
    assert tool.risk_level is RiskLevel.MEDIUM
    assert tool.requires_approval is False
    assert "memory:write" in tool.required_permissions


async def test_remember_surfaces_writer_errors() -> None:
    async def writer(payload: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("Refusing to store memory: looks like a token")

    result = await MemoryWriteTool().run({"content": "x"}, _context(remember=writer))
    assert not result.ok
    assert "Refusing" in (result.error or "")


async def test_recall_returns_snippets() -> None:
    async def search(query: str, limit: int) -> list[dict[str, Any]]:
        assert limit == 3
        return [{"layer": "semantic", "content": "use pgvector", "score": 0.8}]

    result = await MemorySearchTool().run(
        {"query": "pgvector", "limit": 3}, _context(recall=search)
    )
    assert result.ok
    assert result.output["query"] == "pgvector"
    assert result.output["results"][0]["content"] == "use pgvector"


async def test_recall_requires_resource() -> None:
    with pytest.raises(ToolExecutionError):
        await MemorySearchTool().execute({"query": "x"}, _context())


async def test_tool_specs_are_metadata_complete() -> None:
    registry = default_tool_registry()
    for slug in ("load_skill", "recall", "remember"):
        spec = registry.get(slug).spec_dict()
        assert spec["description"]
        assert spec["parameters"]["type"] == "object"
        assert spec["required_permissions"]


def _spec(**kwargs: Any) -> AgentSpec:
    defaults: dict[str, Any] = {
        "slug": "s",
        "name": "S",
        "system_prompt": "p",
        "model": "m",
    }
    defaults.update(kwargs)
    return AgentSpec(**defaults)


def test_agent_spec_carries_resources_into_tool_context() -> None:
    async def loader(slug: str) -> str:
        return slug

    spec = _spec(resources={"load_skill": loader})
    context = spec.tool_context()
    assert context.resources["load_skill"] is loader


def test_agent_spec_without_resources_gets_an_empty_dict() -> None:
    context = _spec().tool_context()
    assert context.resources == {}
