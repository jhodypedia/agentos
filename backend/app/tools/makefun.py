"""make.fun tools: discovering competitions and preparing entries.

The split between the tools mirrors the platform's own rule: nothing here can
put an entry on the board on its own.

* ``makefun_competitions`` and ``makefun_competition_details`` only read what
  the last sync stored, plus whatever the requirements refresh returned. They
  never guess a reward, a deadline or an "open" status.
* ``makefun_prepare_entry`` records the payload and opens an approval; it does
  not call make.fun.
* ``makefun_submit_entry`` is ``critical``, so the runtime refuses it unless a
  human has approved that exact tool for the run. Even then it re-checks the
  entry's own approval record, so the two gates cannot be collapsed into one.

The tools hold no session: the execution layer injects awaitable callables via
``ToolContext.resources``, matching the memory and skill tools.
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any, ClassVar

from app.core.exceptions import AppError
from app.db.enums import ExecutionEnvironment, RiskLevel
from app.tools.base import Tool, ToolContext
from app.tools.errors import ToolExecutionError

_LIST_DEFAULT = 20
_MAX_LIST = 50


def _resource(context: ToolContext, name: str) -> Any:
    value = context.resources.get(name)
    if not callable(value):
        raise ToolExecutionError(f"make.fun access is not available in this run ({name})")
    return value


async def _call(awaitable: Awaitable[dict[str, Any]]) -> dict[str, Any]:
    """Run a resource callable, reporting domain refusals as tool failures.

    A closed competition or a missing approval is a normal answer, not a crash,
    and the model should see the reason rather than an unhandled exception.
    """
    try:
        return await awaitable
    except AppError as exc:
        raise ToolExecutionError(exc.message) from exc


class CompetitionsTool(Tool):
    slug = "makefun_competitions"
    name = "List make.fun competitions"
    description = (
        "List the make.fun competitions the platform has synced, with reward, "
        "deadline and status. Optionally refresh the board from the API first. "
        "Only what the API returned is reported."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "refresh": {
                "type": "boolean",
                "default": False,
                "description": "Fetch the board from make.fun before listing",
            },
            "status": {
                "type": "string",
                "enum": ["draft", "open", "closed", "resolved"],
                "description": "Only return listings in this state",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": _MAX_LIST,
                "default": _LIST_DEFAULT,
            },
        },
        "additionalProperties": False,
    }
    required_permissions: ClassVar[tuple[str, ...]] = ("tool:read",)
    risk_level = RiskLevel.LOW
    execution_environment = ExecutionEnvironment.HOST
    timeout_seconds = 45

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        lister = _resource(context, "makefun_competitions")
        return await _call(
            lister(
                refresh=bool(arguments.get("refresh", False)),
                status=arguments.get("status"),
                limit=min(_MAX_LIST, int(arguments.get("limit", _LIST_DEFAULT))),
            )
        )


class CompetitionDetailsTool(Tool):
    slug = "makefun_competition_details"
    name = "Inspect a make.fun competition"
    description = (
        "Return one competition's published requirements: required deliverables, "
        "validation checks with their fixes, deadlines, entry quota and payout "
        "curve. Pass refresh=true to fetch the latest copy from make.fun."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "slug": {
                "type": "string",
                "minLength": 1,
                "maxLength": 160,
                "description": "The make.fun competition slug, e.g. 'tiny-chess-01'",
            },
            "refresh": {
                "type": "boolean",
                "default": False,
                "description": "Fetch the requirements document from make.fun first",
            },
        },
        "required": ["slug"],
        "additionalProperties": False,
    }
    required_permissions: ClassVar[tuple[str, ...]] = ("tool:read",)
    risk_level = RiskLevel.LOW
    execution_environment = ExecutionEnvironment.HOST
    timeout_seconds = 45

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        loader = _resource(context, "makefun_competition_details")
        return await _call(
            loader(str(arguments["slug"]), refresh=bool(arguments.get("refresh", False)))
        )


class PrepareEntryTool(Tool):
    slug = "makefun_prepare_entry"
    name = "Prepare a make.fun entry"
    description = (
        "Record an entry payload (repository URL, commit SHA, title) and request "
        "human approval to submit it. This does not submit anything. The "
        "transcript paths must point at transcripts that were really produced in "
        "this workspace."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "slug": {
                "type": "string",
                "minLength": 1,
                "maxLength": 160,
                "description": "The make.fun competition slug",
            },
            "repo_url": {
                "type": "string",
                "minLength": 1,
                "maxLength": 1000,
                "description": "Public repository URL",
            },
            "commit_sha": {
                "type": "string",
                "pattern": "^[0-9a-f]{40}$",
                "description": "Full 40-character commit SHA on a branch of the repo",
            },
            "title": {"type": "string", "maxLength": 200},
            "transcript_paths": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 32,
                "description": "Workspace-relative paths of the coding-session transcripts",
            },
        },
        "required": ["slug", "repo_url", "commit_sha"],
        "additionalProperties": False,
    }
    required_permissions: ClassVar[tuple[str, ...]] = ("tool:read", "bounty:write")
    risk_level = RiskLevel.MEDIUM
    execution_environment = ExecutionEnvironment.HOST
    timeout_seconds = 30

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        preparer = _resource(context, "makefun_prepare_entry")
        return await _call(
            preparer(
                slug=str(arguments["slug"]),
                repo_url=str(arguments["repo_url"]),
                commit_sha=str(arguments["commit_sha"]),
                title=str(arguments.get("title") or ""),
                transcript_paths=list(arguments.get("transcript_paths") or []),
            )
        )


class SubmitEntryTool(Tool):
    slug = "makefun_submit_entry"
    name = "Submit a make.fun entry"
    description = (
        "Submit a prepared entry to make.fun. Requires human approval both for "
        "this tool and for the entry's own approval record. A submission is "
        "public and cannot be withdrawn, so never call it without being asked to."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "entry_id": {
                "type": "string",
                "format": "uuid",
                "description": "The prepared entry id returned by makefun_prepare_entry",
            }
        },
        "required": ["entry_id"],
        "additionalProperties": False,
    }
    required_permissions: ClassVar[tuple[str, ...]] = ("tool:read", "bounty:submit")
    risk_level = RiskLevel.CRITICAL
    execution_environment = ExecutionEnvironment.HOST
    timeout_seconds = 90

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        submitter = _resource(context, "makefun_submit_entry")
        return await _call(submitter(str(arguments["entry_id"])))


MAKEFUN_TOOLS: tuple[type[Tool], ...] = (
    CompetitionsTool,
    CompetitionDetailsTool,
    PrepareEntryTool,
    SubmitEntryTool,
)
