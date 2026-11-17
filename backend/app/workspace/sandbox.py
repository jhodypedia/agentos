"""Sandboxed command execution.

Commands run with their working directory pinned inside the workspace, a scrubbed
environment, a wall-clock timeout and an output cap. That is *containment of the
obvious mistakes* — a wrong path, a runaway loop, a command that prints a
gigabyte — not a security boundary against deliberately hostile code. A real
boundary needs a container or VM, which is what ``SandboxRunner`` switches to
when Docker is available.

Two runners exist so the platform degrades honestly rather than pretending:

* :class:`LocalRunner` runs on the host. Fast, and safe enough for trusted
  commands, but it can read anything the API process can read.
* :class:`DockerRunner` runs the command inside a throwaway container with the
  workspace bind-mounted at ``/workspace``, a memory cap, a pid cap, no network
  unless explicitly allowed, and the container removed afterwards.

:func:`resolve_runner` picks the Docker runner when ``SANDBOX_ENABLED`` is on and
the Docker CLI answers, and otherwise reports precisely why it fell back.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import structlog

from app.core.exceptions import ServiceUnavailableError
from app.workspace.paths import Workspace, WorkspaceError

logger = structlog.get_logger("agentos.sandbox")

DEFAULT_TIMEOUT_SECONDS = 120
MAX_TIMEOUT_SECONDS = 600
DEFAULT_MAX_OUTPUT_CHARS = 20_000

# Variables that must never reach a sandboxed command: they carry the platform's
# own credentials, and a shell command is exactly where a leak becomes permanent.
_BLOCKED_ENV_PREFIXES = (
    "DATABASE_",
    "REDIS_",
    "SECRET_",
    "ENCRYPTION_",
    "BOOTSTRAP_",
    "POSTGRES_",
    "AWS_",
    "GITHUB_",
    "GH_",
    "OPENAI_",
    "ANTHROPIC_",
    "GOOGLE_",
    "TELEGRAM_",
)
_BLOCKED_ENV_EXACT = {"SECRET_KEY", "ENCRYPTION_KEY", "PATH_HASH_SEED"}

_SAFE_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "TERM": "dumb",
    # HOME/TMPDIR point at a world-writable directory on purpose: a sandboxed
    # command must not inherit the API user's home, and it has nothing private to
    # put there. This is a deliberate containment choice, not a temp-file path.
    "HOME": "/tmp",  # noqa: S108
    "TMPDIR": "/tmp",  # noqa: S108
    "CI": "1",
    "NO_COLOR": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONUNBUFFERED": "1",
}


@dataclass(slots=True)
class CommandResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False
    truncated: bool = False
    sandbox: str = "local"

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def as_dict(self) -> dict[str, object]:
        return {
            "command": self.command,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_ms": self.duration_ms,
            "timed_out": self.timed_out,
            "truncated": self.truncated,
            "sandbox": self.sandbox,
            "ok": self.ok,
        }


def scrubbed_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    """The environment a sandboxed command receives."""
    env = dict(_SAFE_ENV)
    for key, value in (extra or {}).items():
        if _is_blocked_env(key):
            continue
        env[key] = str(value)
    return env


def _is_blocked_env(key: str) -> bool:
    if key in _BLOCKED_ENV_EXACT:
        return True
    return any(key.startswith(prefix) for prefix in _BLOCKED_ENV_PREFIXES)


class CommandRunner(Protocol):
    name: str

    async def run(
        self,
        command: str,
        *,
        workspace: Workspace,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
        environment: dict[str, str] | None = None,
    ) -> CommandResult: ...


@dataclass(slots=True)
class LocalRunner:
    """Execute on the host with a pared environment and a hard timeout."""

    name: str = "local"
    max_timeout_seconds: int = MAX_TIMEOUT_SECONDS

    async def run(
        self,
        command: str,
        *,
        workspace: Workspace,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
        environment: dict[str, str] | None = None,
    ) -> CommandResult:
        timeout = max(1, min(int(timeout_seconds), self.max_timeout_seconds))
        started = asyncio.get_running_loop().time()
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=str(workspace.root),
            env=scrubbed_environment(environment),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        timed_out = False
        try:
            stdout_b, stderr_b = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError:
            timed_out = True
            _kill_process_group(process)
            stdout_b, stderr_b = await process.communicate()
        duration = int((asyncio.get_running_loop().time() - started) * 1000)

        stdout, out_truncated = _decode(stdout_b, max_output_chars)
        stderr, err_truncated = _decode(stderr_b, max_output_chars)
        return CommandResult(
            command=command,
            exit_code=process.returncode if process.returncode is not None else -1,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration,
            timed_out=timed_out,
            truncated=out_truncated or err_truncated,
            sandbox=self.name,
        )


@dataclass(slots=True)
class DockerRunner:
    """Execute inside a throwaway container with the workspace mounted."""

    image: str
    memory_limit: str = "1g"
    cpu_limit: float = 1.0
    pids_limit: int = 256
    network: str = "none"
    max_timeout_seconds: int = MAX_TIMEOUT_SECONDS
    # Run as the workspace's owner so files the container writes are readable and
    # removable by the host afterwards. The container still has no capabilities
    # and no route out, so this grants no authority the owner did not already
    # have over its own workspace.
    run_as_owner: bool = True
    name: str = "docker"

    async def run(
        self,
        command: str,
        *,
        workspace: Workspace,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
        environment: dict[str, str] | None = None,
    ) -> CommandResult:
        timeout = max(1, min(int(timeout_seconds), self.max_timeout_seconds))
        started = asyncio.get_running_loop().time()

        argv = [
            "docker",
            "run",
            "--rm",
            "--network",
            self.network,
            "--memory",
            self.memory_limit,
            "--cpus",
            str(self.cpu_limit),
            "--pids-limit",
            str(self.pids_limit),
            "--security-opt",
            "no-new-privileges",
            "--cap-drop",
            "ALL",
            "--workdir",
            "/workspace",
            "--volume",
            f"{workspace.root}:/workspace",
            # Tmpfs keeps /tmp inside the container's memory, so a command
            # cannot fill the host's disk from a directory it can write to.
            "--tmpfs",
            # Container-internal mount point, not a host temp path.
            "/tmp:rw,noexec,nosuid,size=256m",  # noqa: S108
            "--read-only",
        ]
        if self.run_as_owner:
            owner = await asyncio.to_thread(_owner_ids, workspace.root)
            if owner is not None:
                argv.extend(["--user", f"{owner[0]}:{owner[1]}"])
        for key, value in scrubbed_environment(environment).items():
            argv.extend(["--env", f"{key}={value}"])
        argv.extend([self.image, "sh", "-lc", command])

        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        timed_out = False
        try:
            stdout_b, stderr_b = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError:
            timed_out = True
            _kill_process_group(process)
            stdout_b, stderr_b = await process.communicate()
        duration = int((asyncio.get_running_loop().time() - started) * 1000)

        stdout, out_truncated = _decode(stdout_b, max_output_chars)
        stderr, err_truncated = _decode(stderr_b, max_output_chars)
        return CommandResult(
            command=command,
            exit_code=process.returncode if process.returncode is not None else -1,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration,
            timed_out=timed_out,
            truncated=out_truncated or err_truncated,
            sandbox=self.name,
        )


def _owner_ids(path: Path) -> tuple[int, int] | None:
    try:
        info = path.stat()
    except OSError:
        return None
    return info.st_uid, info.st_gid


def _decode(payload: bytes | None, max_chars: int) -> tuple[str, bool]:
    text = (payload or b"").decode("utf-8", errors="replace")
    if len(text) <= max_chars:
        return text, False
    head = text[: max_chars // 2]
    tail = text[-(max_chars // 2) :]
    return f"{head}\n... [{len(text) - max_chars} characters omitted] ...\n{tail}", True


def _kill_process_group(process: asyncio.subprocess.Process) -> None:
    """Kill the whole process group so a shell's children die with it."""
    if process.returncode is not None:
        return
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        process.kill()


@dataclass(slots=True)
class SandboxReport:
    """Why a particular runner was chosen — surfaced in tool output and logs."""

    runner: CommandRunner
    requested: str
    reason: str = ""
    docker_available: bool = False
    notes: list[str] = field(default_factory=list)


async def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        process = await asyncio.create_subprocess_exec(
            "docker",
            "version",
            "--format",
            "{{.Server.Version}}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=5)
    except (OSError, TimeoutError):
        return False
    return process.returncode == 0 and bool(stdout.strip())


async def docker_image_available(image: str) -> bool:
    """Check the sandbox image exists locally before promising a sandbox."""
    if shutil.which("docker") is None:
        return False
    try:
        process = await asyncio.create_subprocess_exec(
            "docker",
            "image",
            "inspect",
            image,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(process.communicate(), timeout=10)
    except (OSError, TimeoutError):
        return False
    return process.returncode == 0


async def resolve_runner(
    *,
    sandbox_enabled: bool,
    sandbox_image: str,
    allow_host_execution: bool = True,
    memory_limit: str = "1g",
    cpu_limit: float = 1.0,
    pids_limit: int = 256,
    max_timeout_seconds: int = 900,
    network: str = "none",
) -> SandboxReport:
    """Choose a runner, explaining the choice rather than silently downgrading."""
    docker_up = sandbox_enabled and await docker_available()
    if docker_up and await docker_image_available(sandbox_image):
        return SandboxReport(
            runner=DockerRunner(
                image=sandbox_image,
                memory_limit=memory_limit,
                cpu_limit=cpu_limit,
                pids_limit=pids_limit,
                network=network,
                max_timeout_seconds=max_timeout_seconds,
            ),
            requested="docker",
            docker_available=True,
            reason=f"docker available; commands run in '{sandbox_image}'",
        )

    if docker_up:
        logger.warning("sandbox_image_missing", image=sandbox_image)

    if sandbox_enabled and not allow_host_execution:
        raise ServiceUnavailableError(
            f"Command execution requires a sandbox, but the '{sandbox_image}' image is not "
            "available and host execution is disabled"
        )

    if not sandbox_enabled:
        reason = "sandboxing disabled; commands run on the host with a scrubbed environment"
    elif docker_up:
        reason = (
            f"docker is available but the '{sandbox_image}' image is missing; commands run on "
            "the host with a scrubbed environment"
        )
    else:
        reason = (
            "Docker requested but unavailable; commands run on the host with a scrubbed environment"
        )
    from app.core.metrics import SANDBOX_FALLBACK

    SANDBOX_FALLBACK.inc(reason="image_missing" if docker_up else "docker_unavailable")
    logger.warning("sandbox_fallback", requested=bool(sandbox_enabled), reason=reason)
    return SandboxReport(
        runner=LocalRunner(max_timeout_seconds=max_timeout_seconds),
        requested="docker" if sandbox_enabled else "local",
        docker_available=docker_up,
        reason=reason,
    )


def ensure_inside(path: Path, workspace: Workspace) -> Path:
    """Guard used by callers that already resolved a path elsewhere."""
    if path != workspace.root and workspace.root not in path.parents:
        raise WorkspaceError(f"'{path}' is outside the workspace")
    return path
