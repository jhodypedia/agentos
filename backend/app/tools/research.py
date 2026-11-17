"""Project-bound hypothesis and report tools. Authorization remains human-owned."""

from __future__ import annotations

from typing import Any, ClassVar

from app.core.exceptions import AppError
from app.db.enums import RiskLevel
from app.tools.base import Tool, ToolContext
from app.tools.errors import ToolExecutionError


class ResearchTool(Tool):
    slug = "research_resource"
    name = "Research resource"
    description = "Invoke a project-bound research resource."
    required_permissions: ClassVar[tuple[str, ...]] = ("tool:read", "research:read")

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        resource = context.resources.get(self.slug)
        if not callable(resource):
            raise ToolExecutionError("Research access is unavailable for this run")
        try:
            result: dict[str, Any] = await resource(**arguments)
            return result
        except (AppError, ValueError) as exc:
            raise ToolExecutionError(str(exc)) from exc


class ResearchScopesTool(ResearchTool):
    slug = "research_scopes"
    name = "Read project research scopes"
    description = "Read recorded authorization and scope for this task's project."
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    risk_level = RiskLevel.LOW


class ResearchHypothesisTool(ResearchTool):
    slug = "research_hypothesis"
    name = "Record a vulnerability hypothesis"
    description = "Record an unconfirmed hypothesis within this project's verified scope."
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "scope_id": {"type": "string", "format": "uuid"},
            "title": {"type": "string", "minLength": 1, "maxLength": 400},
            "hypothesis": {"type": "string", "minLength": 10, "maxLength": 20000},
        },
        "required": ["scope_id", "title", "hypothesis"],
        "additionalProperties": False,
    }
    risk_level = RiskLevel.MEDIUM
    required_permissions: ClassVar[tuple[str, ...]] = ("tool:read", "research:write")


class ResearchReportTool(ResearchTool):
    slug = "research_report"
    name = "Generate finding report"
    description = "Generate a local report with actual status and hashed evidence; does not submit."
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"finding_id": {"type": "string", "format": "uuid"}},
        "required": ["finding_id"],
        "additionalProperties": False,
    }
    risk_level = RiskLevel.LOW


RESEARCH_TOOLS: tuple[type[Tool], ...] = (
    ResearchScopesTool,
    ResearchHypothesisTool,
    ResearchReportTool,
)
