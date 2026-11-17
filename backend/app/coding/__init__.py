"""Coding engines: an abstraction the orchestrator talks to by capability.

A coding engine turns "make this change in this repository" into real file and
process activity. The orchestrator must not care which engine does it, so an
engine is defined by a small protocol — ``available``, ``describe`` and
``run`` — and selected by name at call time.

Two engines ship:

* :class:`OpencodeEngine` drives the ``opencode`` CLI (``opencode run --format
  json``) inside the workspace. It is the primary engine because it brings its
  own model loop, edit tools and session transcripts.
* :class:`ShellEngine` is a deliberately minimal fallback: it runs a command the
  caller supplies in the sandbox. It does no reasoning at all, so it exists for
  scripted tasks and as an honest answer when no coding model is configured.

Sessions are the point of the abstraction as much as the model is. A competition
submission sometimes requires the original transcript, so every engine reports a
``session_id`` and a transcript path that the platform records rather than
regenerating.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import structlog

from app.core.exceptions import NotFoundError, UnprocessableError
from app.workspace.paths import Workspace
from app.workspace.sandbox import (
    DEFAULT_MAX_OUTPUT_CHARS,
    DEFAULT_TIMEOUT_SECONDS,
    CommandResult,
    CommandRunner,
    LocalRunner,
)

logger = structlog.get_logger("agentos.coding")

DEFAULT_MAX_TURNS = 30


@dataclass(slots=True)
class CodingRequest:
    """What an engine is asked to do."""

    instruction: str
    workspace: Workspace
    model: str | None = None
    session_id: str | None = None
    max_turns: int = DEFAULT_MAX_TURNS
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    files: list[str] = field(default_factory=list)


@dataclass(slots=True)
class CodingResult:
    engine: str
    ok: bool
    session_id: str | None
    summary: str
    changes: list[dict[str, Any]] = field(default_factory=list)
    commands: list[dict[str, Any]] = field(default_factory=list)
    transcript_path: str | None = None
    duration_ms: int = 0
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "ok": self.ok,
            "session_id": self.session_id,
            "summary": self.summary,
            "changes": self.changes,
            "commands": self.commands,
            "transcript_path": self.transcript_path,
            "duration_ms": self.duration_ms,
            "exit_code": self.exit_code,
            "error": self.error,
        }


class CodingEngine(Protocol):
    name: str

    async def available(self) -> bool: ...

    def describe(self) -> dict[str, Any]: ...

    async def run(self, request: CodingRequest, runner: CommandRunner) -> CodingResult: ...


def _changes_from_git(workspace: Workspace) -> list[dict[str, Any]]:
    """Summarise the working tree without running git."""
    import shutil
    import subprocess

    git = shutil.which("git")
    if git is None:
        return []
    try:
        # Fixed argv (resolved absolute path, no shell, no user input): this
        # reads the workspace's own git status and nothing else.
        completed = subprocess.run(  # noqa: S603
            [git, "status", "--porcelain"],
            cwd=str(workspace.root),
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if completed.returncode != 0:
        return []
    changes: list[dict[str, Any]] = []
    for line in completed.stdout.splitlines():
        if len(line) < 4:
            continue
        status, path = line[:2].strip(), line[3:].strip()
        changes.append({"status": status or "?", "path": path})
    return changes


class OpencodeEngine:
    """Drive the ``opencode`` CLI as the coding engine."""

    name = "opencode"

    def __init__(self, binary: str = "opencode", agent: str | None = None) -> None:
        self.binary = binary
        self.agent = agent

    async def available(self) -> bool:
        if shutil.which(self.binary) is None:
            return False
        try:
            process = await asyncio.create_subprocess_exec(
                self.binary,
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(process.communicate(), timeout=10)
        except (OSError, TimeoutError):
            return False
        return process.returncode == 0

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "binary": self.binary,
            "agent": self.agent,
            "kind": "agentic",
            "capabilities": [
                "edit-files",
                "run-commands",
                "sessions",
                "transcripts",
            ],
        }

    async def run(self, request: CodingRequest, runner: CommandRunner) -> CodingResult:
        if not await self.available():
            raise NotFoundError(f"The '{self.binary}' coding engine is not installed")

        argv = [
            self.binary,
            "run",
            "--format",
            "json",
            "--dir",
            str(request.workspace.root),
        ]
        if request.model:
            argv.extend(["--model", request.model])
        if self.agent:
            argv.extend(["--agent", self.agent])
        if request.session_id:
            argv.extend(["--session", request.session_id])
        for path in request.files:
            argv.extend(["--file", path])
        argv.append(request.instruction)

        started = asyncio.get_running_loop().time()
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(request.workspace.root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        timed_out = False
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                process.communicate(), timeout=max(1, request.timeout_seconds)
            )
        except TimeoutError:
            timed_out = True
            process.kill()
            stdout_b, stderr_b = await process.communicate()
        duration = int((asyncio.get_running_loop().time() - started) * 1000)

        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")[:DEFAULT_MAX_OUTPUT_CHARS]
        session_id, summary = _parse_opencode_events(stdout)

        transcript = await asyncio.to_thread(
            _write_transcript, request.workspace, session_id, stdout
        )

        if timed_out:
            return CodingResult(
                engine=self.name,
                ok=False,
                session_id=session_id,
                summary="The coding engine exceeded its time limit",
                duration_ms=duration,
                transcript_path=transcript,
                stderr=stderr,
                error=f"timed out after {request.timeout_seconds}s",
                changes=_changes_from_git(request.workspace),
            )

        exit_code = process.returncode if process.returncode is not None else -1
        return CodingResult(
            engine=self.name,
            ok=exit_code == 0,
            session_id=session_id,
            summary=summary,
            duration_ms=duration,
            transcript_path=transcript,
            exit_code=exit_code,
            stdout=stdout[-DEFAULT_MAX_OUTPUT_CHARS:],
            stderr=stderr,
            error=None if exit_code == 0 else f"engine exited with {exit_code}",
            changes=_changes_from_git(request.workspace),
        )


def _parse_opencode_events(stdout: str) -> tuple[str | None, str]:
    """Extract the session id and final assistant text from JSONL output.

    ``opencode run --format json`` emits one JSON object per line. Anything
    unparseable is ignored rather than raising: a partially corrupt log should
    still yield the changes the engine made.
    """
    session_id: str | None = None
    texts: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if session_id is None:
            for key in ("sessionID", "session_id", "sessionId"):
                value = event.get(key)
                if isinstance(value, str) and value:
                    session_id = value
                    break
            properties = event.get("properties")
            if session_id is None and isinstance(properties, dict):
                value = properties.get("sessionID")
                if isinstance(value, str) and value:
                    session_id = value
        part = event.get("part")
        if isinstance(part, dict) and part.get("type") == "text":
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                texts.append(text.strip())
                continue
        if event.get("type") == "text" and isinstance(event.get("text"), str):
            texts.append(str(event["text"]).strip())
    summary = texts[-1] if texts else ""
    return session_id, summary[:4000]


def _write_transcript(workspace: Workspace, session_id: str | None, payload: str) -> str | None:
    """Persist the raw engine output inside the workspace for later inspection."""
    if not payload.strip():
        return None
    directory = workspace.root / ".agentos" / "transcripts"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        name = f"{session_id or 'run'}.jsonl"
        target = directory / name
        target.write_text(payload, encoding="utf-8")
    except OSError:
        return None
    return target.relative_to(workspace.root).as_posix()


class ShellEngine:
    """Run a caller-supplied command. No model, no reasoning."""

    name = "shell"

    async def available(self) -> bool:
        return True

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": "none",
            "capabilities": ["run-commands"],
        }

    async def run(self, request: CodingRequest, runner: CommandRunner) -> CodingResult:
        result: CommandResult = await runner.run(
            request.instruction,
            workspace=request.workspace,
            timeout_seconds=request.timeout_seconds,
            max_output_chars=DEFAULT_MAX_OUTPUT_CHARS,
        )
        return CodingResult(
            engine=self.name,
            ok=result.ok,
            session_id=None,
            summary=result.stdout.strip()[:4000],
            duration_ms=result.duration_ms,
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            error=None if result.ok else (result.stderr.strip() or "command failed"),
            changes=_changes_from_git(request.workspace),
        )


class CodingEngineRegistry:
    """Named engines, resolved lazily so an absent binary is not an error."""

    def __init__(
        self, engines: dict[str, CodingEngine] | None = None, default: str = "opencode"
    ) -> None:
        self._engines: dict[str, CodingEngine] = dict(engines or {})
        self.default = default
        if not self._engines:
            self.register(OpencodeEngine())
            self.register(ShellEngine())

    def register(self, engine: CodingEngine) -> None:
        self._engines[engine.name] = engine

    def get(self, name: str | None = None) -> CodingEngine:
        key = name or self.default
        engine = self._engines.get(key)
        if engine is None:
            raise NotFoundError(
                f"Unknown coding engine '{key}'; available: {', '.join(sorted(self._engines))}"
            )
        return engine

    async def resolve(self, name: str | None = None) -> CodingEngine:
        """Return the requested engine, falling back to any available one."""
        try:
            engine = self.get(name)
        except NotFoundError:
            raise
        if await engine.available():
            return engine

        for candidate in self._engines.values():
            if await candidate.available():
                logger.warning(
                    "coding_engine_fallback",
                    requested=engine.name,
                    using=candidate.name,
                )
                return candidate
        raise UnprocessableError(
            "No coding engine is available; install one or configure a shell command"
        )

    async def describe_all(self) -> list[dict[str, Any]]:
        described: list[dict[str, Any]] = []
        for engine in self._engines.values():
            spec = engine.describe()
            spec["available"] = await engine.available()
            spec["default"] = engine.name == self.default
            described.append(spec)
        return described


def default_engine_registry(default: str = "opencode") -> CodingEngineRegistry:
    return CodingEngineRegistry(default=default)


def default_runner() -> CommandRunner:
    return LocalRunner()


def workspace_transcript_dir(workspace: Workspace) -> Path:
    return workspace.root / ".agentos" / "transcripts"
