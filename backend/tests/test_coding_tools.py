"""Workspace tools: filesystem, search, terminal and git through ToolContext."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from app.db.enums import RiskLevel
from app.tools.base import ToolContext
from app.tools.coding import (
    CODING_TOOLS,
    DeleteFileTool,
    GitTool,
    ListFilesTool,
    ReadFileTool,
    RunCommandTool,
    SearchCodeTool,
    WriteFileTool,
)
from app.workspace.paths import Workspace
from app.workspace.sandbox import LocalRunner


@pytest.fixture
async def workspace(tmp_path: Path) -> Workspace:
    return await Workspace.create(tmp_path / "ws")


@pytest.fixture
def context(workspace: Workspace) -> ToolContext:
    return ToolContext(
        permissions=frozenset({"*"}),
        approvals=frozenset({"*"}),
        resources={"workspace": workspace, "command_runner": LocalRunner()},
    )


def test_tool_metadata() -> None:
    slugs = {tool.slug for tool in CODING_TOOLS}
    assert slugs == {
        "read_file",
        "list_files",
        "search_code",
        "write_file",
        "delete_file",
        "run_command",
        "git",
    }
    for tool in CODING_TOOLS:
        assert tool.parameters["type"] == "object"


def test_risk_levels_are_split_correctly() -> None:
    assert ReadFileTool.risk_level is RiskLevel.LOW
    assert ListFilesTool.risk_level is RiskLevel.LOW
    assert SearchCodeTool.risk_level is RiskLevel.LOW
    for tool in (WriteFileTool, DeleteFileTool, RunCommandTool, GitTool):
        assert tool.risk_level is RiskLevel.HIGH


def test_permissions_are_declared() -> None:
    assert ReadFileTool.required_permissions == ("filesystem:read",)
    assert WriteFileTool.required_permissions == ("filesystem:write",)
    assert RunCommandTool.required_permissions == ("tools:execute",)


async def test_read_file_tool(workspace: Workspace, context: ToolContext) -> None:
    await workspace.write_bytes("a.txt", b"hello\nworld\n")
    result = await ReadFileTool().run({"path": "a.txt"}, context)
    assert result.ok
    assert result.output["lines"] == 2
    assert "hello" in result.output["content"]


async def test_read_file_tool_refuses_traversal(context: ToolContext) -> None:
    result = await ReadFileTool().run({"path": "../x"}, context)
    assert not result.ok
    assert "escapes" in (result.error or "") or "traversal" in (result.error or "")


async def test_read_file_tool_without_workspace() -> None:
    result = await ReadFileTool().run({"path": "a.txt"}, ToolContext(permissions=frozenset({"*"})))
    assert not result.ok
    assert "No workspace" in (result.error or "")


async def test_list_files_tool(workspace: Workspace, context: ToolContext) -> None:
    await workspace.write_bytes("src/a.py", b"x")
    result = await ListFilesTool().run({}, context)
    assert result.ok
    assert result.output["count"] >= 2


async def test_write_file_tool(workspace: Workspace, context: ToolContext) -> None:
    result = await WriteFileTool().run({"path": "new/dir/f.txt", "content": "data"}, context)
    assert result.ok
    assert result.output["created"] is True
    assert (workspace.root / "new/dir/f.txt").read_text() == "data"


async def test_write_file_tool_overwrites(workspace: Workspace, context: ToolContext) -> None:
    await workspace.write_bytes("f.txt", b"old")
    result = await WriteFileTool().run({"path": "f.txt", "content": "new"}, context)
    assert result.output["created"] is False
    assert (workspace.root / "f.txt").read_text() == "new"


async def test_delete_file_tool(workspace: Workspace, context: ToolContext) -> None:
    await workspace.write_bytes("gone.txt", b"x")
    result = await DeleteFileTool().run({"path": "gone.txt"}, context)
    assert result.ok
    assert not (workspace.root / "gone.txt").exists()


async def test_delete_file_tool_refuses_root(context: ToolContext) -> None:
    result = await DeleteFileTool().run({"path": "."}, context)
    assert not result.ok


async def test_search_code_tool(workspace: Workspace, context: ToolContext) -> None:
    await workspace.write_bytes("a.py", b"def target():\n    pass\n")
    result = await SearchCodeTool().run({"pattern": "def target"}, context)
    assert result.ok
    assert result.output["count"] == 1
    assert result.output["matches"][0]["path"] == "a.py"


async def test_search_code_tool_rejects_bad_regex(context: ToolContext) -> None:
    result = await SearchCodeTool().run({"pattern": "(unclosed"}, context)
    assert not result.ok
    assert "Invalid regular expression" in (result.error or "")


async def test_run_command_tool(workspace: Workspace, context: ToolContext) -> None:
    await workspace.write_bytes("marker.txt", b"x")
    result = await RunCommandTool().run({"command": "ls"}, context)
    assert result.ok
    assert "marker.txt" in result.output["stdout"]
    assert result.output["sandbox"] == "local"


async def test_run_command_tool_requires_runner(workspace: Workspace) -> None:
    result = await RunCommandTool().run(
        {"command": "ls"},
        ToolContext(
            permissions=frozenset({"*"}),
            approvals=frozenset({"*"}),
            resources={"workspace": workspace},
        ),
    )
    assert not result.ok
    assert "command runner" in (result.error or "")


async def test_high_risk_tools_need_permission_and_approval(workspace: Workspace) -> None:
    from app.tools.errors import ApprovalRequiredError, ToolPermissionError

    bare = ToolContext(resources={"workspace": workspace})
    with pytest.raises(ToolPermissionError):
        await WriteFileTool().run({"path": "a.txt", "content": "x"}, bare)

    unapproved = ToolContext(
        permissions=frozenset({"*"}),
        resources={"workspace": workspace, "command_runner": LocalRunner()},
    )
    with pytest.raises(ApprovalRequiredError):
        await RunCommandTool().run({"command": "true"}, unapproved)

    from app.tools.errors import ToolPermissionError as _Denied

    read_only = ToolContext(permissions=frozenset({"filesystem:read"}))
    with pytest.raises(_Denied):
        await DeleteFileTool().run({"path": "a.txt"}, read_only)


async def test_run_command_tool_reports_failure(context: ToolContext) -> None:
    result = await RunCommandTool().run({"command": "exit 2"}, context)
    assert result.ok
    assert result.output["ok"] is False
    assert result.output["exit_code"] == 2


async def test_git_tool_read_only_actions(context: ToolContext, workspace: Workspace) -> None:
    _git(workspace, "init", "-q")
    await workspace.write_bytes("f.txt", b"x")
    for action in ("status", "diff", "log", "branch"):
        result = await GitTool().run({"action": action}, context)
        assert result.output["action"] == action
        assert result.output["read_only"] is True


async def test_git_tool_commit_requires_message(context: ToolContext, workspace: Workspace) -> None:
    _git(workspace, "init", "-q")
    result = await GitTool().run({"action": "commit"}, context)
    assert not result.ok
    assert "commit message" in (result.error or "")


async def test_git_tool_rejects_action_outside_the_enum(context: ToolContext) -> None:
    from app.tools.errors import ToolValidationError

    with pytest.raises(ToolValidationError):
        await GitTool().run({"action": "push"}, context)


async def test_git_tool_add_and_commit(context: ToolContext, workspace: Workspace) -> None:
    _git(workspace, "init", "-q")
    _git(workspace, "config", "user.email", "t@e.st")
    _git(workspace, "config", "user.name", "T")
    _git(
        workspace,
        "-c",
        "user.email=t@e.st",
        "-c",
        "user.name=T",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "init",
    )
    await workspace.write_bytes("new.txt", b"content\n")
    assert (await GitTool().run({"action": "add"}, context)).output["exit_code"] == 0
    committed = await GitTool().run({"action": "commit", "message": "add new file"}, context)
    assert committed.output["exit_code"] == 0
    assert committed.output["read_only"] is False


async def test_tool_argument_validation(context: ToolContext) -> None:
    from app.tools.errors import ToolValidationError

    with pytest.raises(ToolValidationError):
        await ReadFileTool().run({}, context)


def _git(workspace: Workspace, *args: Any) -> None:
    git = shutil.which("git") or "git"
    # Test-owned temporary repository, resolved absolute binary, fixed argv.
    subprocess.run(  # noqa: S603
        [git, *args], cwd=workspace.root, check=True, capture_output=True
    )
