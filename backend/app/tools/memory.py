"""Tools for on-demand skill loading and memory access.

These tools hold no database session. The execution layer injects awaitable
callables through ``ToolContext.resources``, which keeps the tool module free of
persistence concerns and lets the same tool run under a test harness with a
stub resource.
"""

from __future__ import annotations

from typing import Any, ClassVar

from app.db.enums import ExecutionEnvironment, RiskLevel
from app.tools.base import Tool, ToolContext
from app.tools.errors import ToolExecutionError


class LoadSkillTool(Tool):
    slug = "load_skill"
    name = "Load skill"
    description = (
        "Load the full instructions for one of your skills by slug. Call this "
        "before doing work the skill covers; the instructions are not available "
        "until you load them."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "slug": {
                "type": "string",
                "description": "Skill slug from your available skills list",
                "minLength": 1,
                "maxLength": 120,
            }
        },
        "required": ["slug"],
        "additionalProperties": False,
    }
    required_permissions: ClassVar[tuple[str, ...]] = ("tool:read",)
    risk_level = RiskLevel.LOW
    execution_environment = ExecutionEnvironment.HOST
    timeout_seconds = 10

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        loader = context.resources.get("load_skill")
        if not callable(loader):
            raise ToolExecutionError("Skill loading is not available in this run")
        slug = str(arguments["slug"]).strip()
        try:
            instructions = await loader(slug)
        except Exception as exc:
            raise ToolExecutionError(str(exc)) from exc
        return {"slug": slug, "instructions": instructions}


class MemoryWriteTool(Tool):
    slug = "remember"
    name = "Remember"
    description = (
        "Store a durable memory for future tasks. Use 'semantic' for reusable "
        "technical knowledge, 'project' for repository-specific facts, 'agent' "
        "for your own preferences, and 'episodic' for what happened on a task. "
        "Never store credentials or API keys."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The fact or lesson to remember, self-contained",
                "minLength": 1,
                "maxLength": 8000,
            },
            "layer": {
                "type": "string",
                "enum": ["semantic", "project", "agent", "episodic"],
                "default": "semantic",
            },
            "key": {
                "type": "string",
                "description": "Optional stable key so later writes supersede this one",
                "maxLength": 200,
            },
            "importance": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.5},
        },
        "required": ["content"],
        "additionalProperties": False,
    }
    required_permissions: ClassVar[tuple[str, ...]] = ("tool:read", "memory:write")
    risk_level = RiskLevel.MEDIUM
    execution_environment = ExecutionEnvironment.HOST
    timeout_seconds = 20

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        writer = context.resources.get("remember")
        if not callable(writer):
            raise ToolExecutionError("Memory writing is not available in this run")
        payload = {
            "content": str(arguments["content"]),
            "layer": str(arguments.get("layer") or "semantic"),
            "key": arguments.get("key"),
            "importance": float(arguments.get("importance", 0.5)),
        }
        try:
            result = await writer(payload)
        except Exception as exc:
            raise ToolExecutionError(str(exc)) from exc
        return dict(result)


class MemorySearchTool(Tool):
    slug = "recall"
    name = "Recall"
    description = (
        "Search your memory for facts relevant to a query. Returns short "
        "snippets with their layer and age."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 2000},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
        },
        "required": ["query"],
        "additionalProperties": False,
    }
    required_permissions: ClassVar[tuple[str, ...]] = ("tool:read",)
    risk_level = RiskLevel.LOW
    execution_environment = ExecutionEnvironment.HOST
    timeout_seconds = 15

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        search = context.resources.get("recall")
        if not callable(search):
            raise ToolExecutionError("Memory search is not available in this run")
        try:
            results = await search(str(arguments["query"]), int(arguments.get("limit", 5)))
        except Exception as exc:
            raise ToolExecutionError(str(exc)) from exc
        return {"query": arguments["query"], "results": results}
