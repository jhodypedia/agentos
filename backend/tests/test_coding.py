"""Coding engines: JSONL parsing, transcripts, registry fallback, shell engine."""

from __future__ import annotations

import json
import shutil
import stat
from pathlib import Path
from typing import Any

import pytest

from app.coding import (
    CodingEngineRegistry,
    CodingRequest,
    OpencodeEngine,
    ShellEngine,
    _parse_opencode_events,
    default_engine_registry,
)
from app.core.exceptions import NotFoundError, UnprocessableError
from app.workspace.paths import Workspace
from app.workspace.sandbox import LocalRunner


@pytest.fixture
async def workspace(tmp_path: Path) -> Workspace:
    return await Workspace.create(tmp_path / "ws")


def test_parse_extracts_session_and_final_text() -> None:
    events = [
        {"type": "step_start", "properties": {"sessionID": "ses_123"}},
        {"part": {"type": "text", "text": "first thought"}, "sessionID": "ses_123"},
        {"part": {"type": "text", "text": "final answer"}, "sessionID": "ses_123"},
    ]
    payload = "\n".join(json.dumps(event) for event in events)
    session_id, summary = _parse_opencode_events(payload)
    assert session_id == "ses_123"
    assert summary == "final answer"


def test_parse_tolerates_garbage_lines() -> None:
    payload = 'not json\n{"part": {"type": "text", "text": "ok"}}\n{oops\n'
    session_id, summary = _parse_opencode_events(payload)
    assert session_id is None
    assert summary == "ok"


def test_parse_returns_empty_when_no_text() -> None:
    session_id, summary = _parse_opencode_events('{"type": "step_start"}\n')
    assert session_id is None
    assert summary == ""


def test_parse_supports_top_level_text_event() -> None:
    payload = json.dumps({"type": "text", "text": "top level", "sessionId": "abc"})
    session_id, summary = _parse_opencode_events(payload)
    assert session_id == "abc"
    assert summary == "top level"


def _stub_opencode(tmp_path: Path, payload: list[dict[str, Any]]) -> str:
    """Write a fake `opencode` executable emitting JSONL and echoing argv."""
    binary = tmp_path / "opencode"
    body = "\n".join(json.dumps(event) for event in payload)
    script = (
        "#!/usr/bin/env python3\n"
        "import sys, json\n"
        "if '--version' in sys.argv:\n"
        "    print('1.0.0'); sys.exit(0)\n"
        "print(json.dumps({'argv': sys.argv[1:]}))\n"
        f"print({body!r})\n"
    )
    binary.write_text(script)
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    return str(binary)


async def test_opencode_engine_reports_unavailable_when_missing(
    tmp_path: Path,
) -> None:
    engine = OpencodeEngine(binary=str(tmp_path / "definitely-missing"))
    assert await engine.available() is False
    with pytest.raises(NotFoundError):
        await engine.run(
            CodingRequest(instruction="x", workspace=await Workspace.create(tmp_path / "w")),
            LocalRunner(),
        )


async def test_opencode_engine_runs_and_records_transcript(tmp_path: Path) -> None:
    workspace = await Workspace.create(tmp_path / "ws")
    binary = _stub_opencode(
        tmp_path, [{"sessionID": "ses_1", "part": {"type": "text", "text": "done"}}]
    )
    engine = OpencodeEngine(binary=binary, agent="build")
    assert await engine.available()

    result = await engine.run(
        CodingRequest(
            instruction="add a test",
            workspace=workspace,
            model="gpt-6-astra",
            timeout_seconds=30,
        ),
        LocalRunner(),
    )
    assert result.ok
    assert result.session_id == "ses_1"
    assert result.summary == "done"
    assert result.exit_code == 0

    assert result.transcript_path is not None
    transcript = workspace.root / result.transcript_path
    assert transcript.is_file()
    assert "ses_1" in transcript.read_text()


async def test_opencode_engine_passes_flags(tmp_path: Path) -> None:
    workspace = await Workspace.create(tmp_path / "ws")
    binary = _stub_opencode(tmp_path, [{"sessionID": "s", "part": {"type": "text", "text": "ok"}}])
    engine = OpencodeEngine(binary=binary)

    result = await engine.run(
        CodingRequest(
            instruction="do it",
            workspace=workspace,
            model="m1",
            session_id="ses_prev",
            timeout_seconds=30,
        ),
        LocalRunner(),
    )
    argv_event = json.loads(result.stdout.splitlines()[0])
    argv = argv_event["argv"]
    assert "--format" in argv and "json" in argv
    assert "--dir" in argv and str(workspace.root) in argv
    assert "--model" in argv and "m1" in argv
    assert "--session" in argv and "ses_prev" in argv
    assert argv[-1] == "do it"


async def test_opencode_engine_reports_failure_exit(tmp_path: Path) -> None:
    workspace = await Workspace.create(tmp_path / "ws")
    binary = tmp_path / "opencode"
    script = (
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "if '--version' in sys.argv:\n"
        "    print('1.0.0'); sys.exit(0)\n"
        "print('boom', file=sys.stderr)\n"
        "sys.exit(4)\n"
    )
    binary.write_text(script)
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)

    result = await OpencodeEngine(binary=str(binary)).run(
        CodingRequest(instruction="x", workspace=workspace, timeout_seconds=30),
        LocalRunner(),
    )
    assert not result.ok
    assert result.exit_code == 4
    assert "boom" in result.stderr


async def test_opencode_engine_detects_changes(tmp_path: Path) -> None:
    workspace = await Workspace.create(tmp_path / "ws")
    binary = _stub_opencode(tmp_path, [{"part": {"type": "text", "text": "edited"}}])
    _init_repo(workspace)
    await workspace.write_bytes("new.txt", b"hello")
    result = await OpencodeEngine(binary=binary).run(
        CodingRequest(instruction="x", workspace=workspace, timeout_seconds=30),
        LocalRunner(),
    )
    paths = [change["path"] for change in result.changes]
    assert "new.txt" in paths


def _init_repo(workspace: Workspace) -> None:
    import subprocess

    git = shutil.which("git") or "git"
    # Fixed argv from a resolved absolute path: initialises the test's own
    # temporary repository and touches nothing else.
    subprocess.run([git, "init", "-q"], cwd=workspace.root, check=True)  # noqa: S603
    subprocess.run(  # noqa: S603
        [
            git,
            "-c",
            "user.email=t@e.st",
            "-c",
            "user.name=T",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "init",
        ],
        cwd=workspace.root,
        check=True,
    )


async def test_shell_engine_runs_command(tmp_path: Path) -> None:
    workspace = await Workspace.create(tmp_path / "ws")
    result = await ShellEngine().run(
        CodingRequest(instruction="echo hello", workspace=workspace, timeout_seconds=20),
        LocalRunner(),
    )
    assert result.ok
    assert "hello" in result.summary
    assert result.session_id is None


async def test_shell_engine_reports_failure(tmp_path: Path) -> None:
    workspace = await Workspace.create(tmp_path / "ws")
    result = await ShellEngine().run(
        CodingRequest(instruction="exit 7", workspace=workspace, timeout_seconds=20),
        LocalRunner(),
    )
    assert not result.ok
    assert result.exit_code == 7


async def test_registry_resolves_by_name() -> None:
    registry = default_engine_registry()
    assert (await registry.resolve("shell")).name == "shell"


async def test_registry_rejects_unknown_engine() -> None:
    registry = default_engine_registry()
    with pytest.raises(NotFoundError):
        await registry.resolve("nope")


async def test_registry_falls_back_when_requested_engine_unavailable(tmp_path: Path) -> None:
    registry = CodingEngineRegistry(default="opencode")
    registry.register(OpencodeEngine(binary=str(tmp_path / "nope")))
    registry.register(ShellEngine())
    assert (await registry.resolve("opencode")).name == "shell"


async def test_registry_raises_when_nothing_available(tmp_path: Path) -> None:
    registry = CodingEngineRegistry(
        engines={"opencode": OpencodeEngine(binary=str(tmp_path / "nope"))}
    )
    with pytest.raises(UnprocessableError):
        await registry.resolve("opencode")


async def test_describe_all_includes_availability() -> None:
    described = await default_engine_registry().describe_all()
    names = {item["name"] for item in described}
    assert names == {"opencode", "shell"}
    for item in described:
        assert isinstance(item["available"], bool)
        assert "capabilities" in item
