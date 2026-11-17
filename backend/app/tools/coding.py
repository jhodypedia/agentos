"""Workspace tools: filesystem, terminal, git and code search.

Each tool resolves every path through :class:`app.workspace.paths.Workspace`, so
confinement is enforced by the same code for all of them. The tool classes hold
no session and no configuration: the execution layer injects a ``workspace``
resource and, for the terminal, a ``command_runner``.

Risk levels are deliberate. Reading and searching are ``low``. Writing,
deleting and running commands are ``high``, which means the runtime refuses them
unless the tool slug appears in the caller's approval set — an agent can plan a
change, but a human has to let it land.
"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

from app.db.enums import ExecutionEnvironment, RiskLevel
from app.tools.base import Tool, ToolContext
from app.tools.errors import ToolExecutionError
from app.workspace.paths import (
    DEFAULT_MAX_FILE_BYTES,
    DEFAULT_QUOTA_BYTES,
    Workspace,
    WorkspaceError,
)
from app.workspace.sandbox import (
    DEFAULT_MAX_OUTPUT_CHARS,
    DEFAULT_TIMEOUT_SECONDS,
    CommandRunner,
)


def _workspace(context: ToolContext) -> Workspace:
    workspace = context.resources.get("workspace")
    if workspace is None:
        raise ToolExecutionError("No workspace is attached to this run")
    if not isinstance(workspace, Workspace):
        raise ToolExecutionError("The attached workspace is invalid")
    return workspace


def _runner(context: ToolContext) -> CommandRunner:
    runner = context.resources.get("command_runner")
    if runner is None:
        raise ToolExecutionError("No command runner is attached to this run")
    return runner  # type: ignore[no-any-return]


def _quota_bytes(context: ToolContext) -> int:
    """Disk quota the execution layer resolved for this run."""
    value = context.metadata.get("workspace_quota_bytes", DEFAULT_QUOTA_BYTES)
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return DEFAULT_QUOTA_BYTES


async def _warn_if_over_quota(workspace: Workspace, quota_bytes: int, result: Any) -> None:
    """Append a warning when a command pushed the workspace past its quota.

    The command has already run, so the only honest response is to say so on
    the result: deleting the files the user asked to build would be worse than
    a loud accounting note.
    """
    used = await workspace.usage_bytes(quota_bytes=quota_bytes)
    if used <= quota_bytes:
        return
    from app.core.metrics import WORKSPACE_QUOTA_REJECTED

    WORKSPACE_QUOTA_REJECTED.inc()
    note = f"workspace quota exceeded after this command: {used} of {quota_bytes} bytes in use"
    stdout = getattr(result, "stdout", "")
    try:
        result.stdout = f"{stdout}\n[{note}]" if stdout else f"[{note}]"
    except AttributeError:  # pragma: no cover - result is always a CommandResult
        return


class ReadFileTool(Tool):
    slug = "read_file"
    name = "Read file"
    description = (
        "Read a UTF-8 text file from the project workspace. Paths are relative to "
        "the workspace root and cannot escape it."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Workspace-relative path"},
            "max_bytes": {
                "type": "integer",
                "minimum": 100,
                "maximum": DEFAULT_MAX_FILE_BYTES,
                "default": 200_000,
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    }
    required_permissions: ClassVar[tuple[str, ...]] = ("filesystem:read",)
    risk_level = RiskLevel.LOW
    execution_environment = ExecutionEnvironment.SANDBOX
    timeout_seconds = 15

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        workspace = _workspace(context)
        raw = str(arguments["path"])
        limit = int(arguments.get("max_bytes", 200_000))
        try:
            payload = await workspace.read_bytes(raw, max_bytes=limit)
        except WorkspaceError as exc:
            raise ToolExecutionError(exc.message) from exc
        text = payload.decode("utf-8", errors="replace")
        lines = text.splitlines()
        return {
            "path": raw,
            "bytes": len(payload),
            "lines": len(lines),
            "content": text,
        }


class ListFilesTool(Tool):
    slug = "list_files"
    name = "List files"
    description = "List files and directories under a workspace-relative path."
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "default": ".", "description": "Directory to list"},
            "max_entries": {"type": "integer", "minimum": 1, "maximum": 5000, "default": 500},
        },
        "required": [],
        "additionalProperties": False,
    }
    required_permissions: ClassVar[tuple[str, ...]] = ("filesystem:read",)
    risk_level = RiskLevel.LOW
    execution_environment = ExecutionEnvironment.SANDBOX
    timeout_seconds = 20

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        workspace = _workspace(context)
        raw = str(arguments.get("path") or ".")
        limit = int(arguments.get("max_entries", 500))
        try:
            entries = await workspace.list_tree(raw, max_entries=limit)
        except WorkspaceError as exc:
            raise ToolExecutionError(exc.message) from exc
        return {"path": raw, "entries": entries, "count": len(entries)}


class WriteFileTool(Tool):
    slug = "write_file"
    name = "Write file"
    description = (
        "Create or overwrite a text file in the project workspace. Requires "
        "approval because it changes the project."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Workspace-relative path"},
            "content": {"type": "string", "description": "Full file content"},
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    }
    required_permissions: ClassVar[tuple[str, ...]] = ("filesystem:write",)
    risk_level = RiskLevel.HIGH
    execution_environment = ExecutionEnvironment.SANDBOX
    timeout_seconds = 20

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        workspace = _workspace(context)
        raw = str(arguments["path"])
        content = str(arguments["content"])
        try:
            existed = await asyncio.to_thread((workspace.root / raw).exists)
            path = await workspace.write_bytes(
                raw, content.encode("utf-8"), quota_bytes=_quota_bytes(context)
            )
        except WorkspaceError as exc:
            raise ToolExecutionError(exc.message) from exc
        return {
            "path": workspace.relative(path),
            "bytes": len(content.encode("utf-8")),
            "created": not existed,
        }


class DeleteFileTool(Tool):
    slug = "delete_file"
    name = "Delete path"
    description = (
        "Delete a file or directory inside the project workspace. Requires "
        "approval because it is destructive."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Workspace-relative path"}},
        "required": ["path"],
        "additionalProperties": False,
    }
    required_permissions: ClassVar[tuple[str, ...]] = ("filesystem:write",)
    risk_level = RiskLevel.HIGH
    execution_environment = ExecutionEnvironment.SANDBOX
    timeout_seconds = 20

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        workspace = _workspace(context)
        raw = str(arguments["path"])
        try:
            path = await workspace.delete(raw)
        except WorkspaceError as exc:
            raise ToolExecutionError(exc.message) from exc
        return {"deleted": workspace.relative(path)}


class SearchCodeTool(Tool):
    slug = "search_code"
    name = "Search code"
    description = (
        "Search the project workspace for a regular expression and return matching "
        "lines with their file and line number."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Python regular expression"},
            "glob": {"type": "string", "default": "**/*", "description": "File glob filter"},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50},
        },
        "required": ["pattern"],
        "additionalProperties": False,
    }
    required_permissions: ClassVar[tuple[str, ...]] = ("filesystem:read",)
    risk_level = RiskLevel.LOW
    execution_environment = ExecutionEnvironment.SANDBOX
    timeout_seconds = 30

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        import re

        workspace = _workspace(context)
        pattern = str(arguments["pattern"])
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ToolExecutionError(f"Invalid regular expression: {exc}") from exc

        results = await workspace.search_text(
            pattern,
            glob=str(arguments.get("glob") or "**/*"),
            max_results=int(arguments.get("max_results", 50)),
        )
        return {"pattern": pattern, "matches": results, "count": len(results)}


class RunCommandTool(Tool):
    slug = "run_command"
    name = "Run command"
    description = (
        "Run a shell command with the project workspace as the working directory. "
        "The environment is scrubbed and the run is time-limited. Requires approval."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Command line to execute"},
            "timeout_seconds": {
                "type": "integer",
                "minimum": 1,
                "maximum": DEFAULT_TIMEOUT_SECONDS,
                "default": 60,
            },
        },
        "required": ["command"],
        "additionalProperties": False,
    }
    required_permissions: ClassVar[tuple[str, ...]] = ("tools:execute",)
    risk_level = RiskLevel.HIGH
    execution_environment = ExecutionEnvironment.SANDBOX
    timeout_seconds = DEFAULT_TIMEOUT_SECONDS + 10

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        workspace = _workspace(context)
        runner = _runner(context)
        quota = _quota_bytes(context)
        try:
            await workspace.check_quota(quota_bytes=quota)
        except WorkspaceError as exc:
            raise ToolExecutionError(exc.message) from exc
        result = await runner.run(
            str(arguments["command"]),
            workspace=workspace,
            timeout_seconds=int(arguments.get("timeout_seconds", 60)),
            max_output_chars=DEFAULT_MAX_OUTPUT_CHARS,
        )
        await _warn_if_over_quota(workspace, quota, result)
        return result.as_dict()


class GitTool(Tool):
    slug = "git"
    name = "Git"
    description = (
        "Inspect or change the project's git state: status, diff, log, branch, "
        "commit, add and checkout. Write operations require approval."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["status", "diff", "log", "branch", "add", "commit", "checkout"],
                "description": "Which git operation to perform",
            },
            "path": {"type": "string", "description": "Path argument for add/checkout"},
            "message": {"type": "string", "description": "Commit message"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
        },
        "required": ["action"],
        "additionalProperties": False,
    }
    required_permissions: ClassVar[tuple[str, ...]] = ("filesystem:write",)
    risk_level = RiskLevel.HIGH
    execution_environment = ExecutionEnvironment.SANDBOX
    timeout_seconds = 60

    _READ_ONLY: ClassVar[frozenset[str]] = frozenset({"status", "diff", "log", "branch"})

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        import shlex

        workspace = _workspace(context)
        runner = _runner(context)
        action = str(arguments["action"])

        if action == "status":
            command = "git status --porcelain=v1 --branch"
        elif action == "diff":
            command = "git diff --stat && git diff"
        elif action == "log":
            command = f"git log --oneline -n {int(arguments.get('limit', 20))}"
        elif action == "branch":
            command = "git rev-parse --abbrev-ref HEAD && git branch --list"
        elif action == "add":
            target = arguments.get("path")
            command = "git add -A" if not target else f"git add -- {shlex.quote(str(target))}"
        elif action == "commit":
            message = str(arguments.get("message") or "").strip()
            if not message:
                raise ToolExecutionError("A commit message is required")
            command = f"git commit -m {shlex.quote(message)}"
        elif action == "checkout":
            target = str(arguments.get("path") or "").strip()
            if not target:
                raise ToolExecutionError("A branch or commit is required for checkout")
            command = f"git checkout {shlex.quote(target)}"
        else:
            raise ToolExecutionError(f"Unsupported git action '{action}'")

        result = await runner.run(
            command,
            workspace=workspace,
            timeout_seconds=self.timeout_seconds - 5,
            max_output_chars=DEFAULT_MAX_OUTPUT_CHARS,
        )
        payload = result.as_dict()
        payload["action"] = action
        payload["read_only"] = action in self._READ_ONLY
        return payload


CODING_TOOLS: tuple[type[Tool], ...] = (
    ReadFileTool,
    ListFilesTool,
    SearchCodeTool,
    WriteFileTool,
    DeleteFileTool,
    RunCommandTool,
    GitTool,
)
