"""Sandbox runners: environment scrubbing, timeouts, output caps, selection."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.core.exceptions import ServiceUnavailableError
from app.workspace.paths import Workspace, WorkspaceError
from app.workspace.sandbox import (
    LocalRunner,
    _is_blocked_env,
    docker_available,
    ensure_inside,
    resolve_runner,
    scrubbed_environment,
)


@pytest.fixture
async def workspace(tmp_path: Path) -> Workspace:
    return await Workspace.create(tmp_path / "ws")


@pytest.mark.parametrize(
    "key",
    [
        "DATABASE_URL",
        "REDIS_URL",
        "SECRET_KEY",
        "ENCRYPTION_KEY",
        "BOOTSTRAP_ADMIN_PASSWORD",
        "AWS_SECRET_ACCESS_KEY",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "OPENAI_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "POSTGRES_PASSWORD",
    ],
)
def test_secrets_are_blocked(key: str) -> None:
    assert _is_blocked_env(key)
    assert key not in scrubbed_environment({key: "leak"})


def test_ordinary_variables_pass_through() -> None:
    env = scrubbed_environment({"MY_FLAG": "1", "PATH": "/custom"})
    assert env["MY_FLAG"] == "1"
    assert env["PATH"] == "/custom"


def test_environment_is_pared_by_default() -> None:
    env = scrubbed_environment()
    assert env["HOME"] == "/tmp"  # noqa: S108 - deliberate sandbox HOME
    assert env["LANG"] == "C.UTF-8"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"


async def test_local_runner_runs_in_workspace(workspace: Workspace) -> None:
    await workspace.write_bytes("hello.txt", b"hi")
    result = await LocalRunner().run("ls", workspace=workspace, timeout_seconds=10)
    assert result.ok
    assert "hello.txt" in result.stdout
    assert result.sandbox == "local"


async def test_local_runner_does_not_leak_secrets(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgres://leaked")
    result = await LocalRunner().run("env", workspace=workspace, timeout_seconds=10)
    assert result.ok
    assert "postgres://leaked" not in result.stdout


async def test_local_runner_reports_nonzero_exit(workspace: Workspace) -> None:
    result = await LocalRunner().run("exit 3", workspace=workspace, timeout_seconds=10)
    assert result.exit_code == 3
    assert not result.ok


async def test_local_runner_enforces_timeout(workspace: Workspace) -> None:
    result = await LocalRunner().run("sleep 5", workspace=workspace, timeout_seconds=1)
    assert result.timed_out
    assert not result.ok


async def test_local_runner_caps_output(workspace: Workspace) -> None:
    command = f"{sys.executable} -c \"print('x' * 20000)\""
    result = await LocalRunner().run(
        command, workspace=workspace, timeout_seconds=20, max_output_chars=500
    )
    assert result.truncated
    assert len(result.stdout) < 20_000
    assert "characters omitted" in result.stdout


async def test_local_runner_captures_stderr(workspace: Workspace) -> None:
    result = await LocalRunner().run("echo oops >&2", workspace=workspace, timeout_seconds=10)
    assert "oops" in result.stderr


async def test_docker_availability_is_boolean() -> None:
    assert isinstance(await docker_available(), bool)


async def test_resolve_runner_falls_back_to_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _unavailable() -> bool:
        return False

    monkeypatch.setattr("app.workspace.sandbox.docker_available", _unavailable)
    report = await resolve_runner(
        sandbox_enabled=True, sandbox_image="img:1", allow_host_execution=True
    )
    assert report.runner.name == "local"
    assert report.docker_available is False
    assert "unavailable" in report.reason


async def test_resolve_runner_refuses_when_host_execution_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _unavailable() -> bool:
        return False

    monkeypatch.setattr("app.workspace.sandbox.docker_available", _unavailable)
    with pytest.raises(ServiceUnavailableError):
        await resolve_runner(
            sandbox_enabled=True, sandbox_image="img:1", allow_host_execution=False
        )


async def test_resolve_runner_prefers_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _available() -> bool:
        return True

    async def _image(image: str) -> bool:
        return True

    monkeypatch.setattr("app.workspace.sandbox.docker_available", _available)
    monkeypatch.setattr("app.workspace.sandbox.docker_image_available", _image)
    report = await resolve_runner(
        sandbox_enabled=True, sandbox_image="img:1", allow_host_execution=True
    )
    assert report.runner.name == "docker"
    assert "img:1" in report.reason


async def test_missing_sandbox_image_falls_back_to_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _available() -> bool:
        return True

    async def _image(image: str) -> bool:
        return False

    monkeypatch.setattr("app.workspace.sandbox.docker_available", _available)
    monkeypatch.setattr("app.workspace.sandbox.docker_image_available", _image)
    report = await resolve_runner(
        sandbox_enabled=True, sandbox_image="missing:1", allow_host_execution=True
    )
    assert report.runner.name == "local"
    assert report.docker_available is True
    assert "image is missing" in report.reason


async def test_missing_sandbox_image_refuses_when_host_execution_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _available() -> bool:
        return True

    async def _image(image: str) -> bool:
        return False

    monkeypatch.setattr("app.workspace.sandbox.docker_available", _available)
    monkeypatch.setattr("app.workspace.sandbox.docker_image_available", _image)
    with pytest.raises(ServiceUnavailableError):
        await resolve_runner(
            sandbox_enabled=True, sandbox_image="missing:1", allow_host_execution=False
        )


async def test_resolve_runner_local_when_sandbox_disabled() -> None:
    report = await resolve_runner(
        sandbox_enabled=False, sandbox_image="img:1", allow_host_execution=True
    )
    assert report.runner.name == "local"


async def test_ensure_inside(workspace: Workspace) -> None:
    inside = workspace.root / "a.txt"
    assert ensure_inside(inside, workspace) == inside
    with pytest.raises(WorkspaceError):
        ensure_inside(Path("/etc/passwd"), workspace)
