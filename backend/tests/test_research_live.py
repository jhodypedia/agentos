"""Opt-in real offline scanner check: RUN_RESEARCH_LIVE=1 uv run pytest ..."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.security_research.scanners import command_for, parse_output
from app.workspace.paths import Workspace
from app.workspace.sandbox import DockerRunner


@pytest.mark.skipif(os.environ.get("RUN_RESEARCH_LIVE") != "1", reason="Opt-in Docker scan")
async def test_real_semgrep_offline(tmp_path: Path) -> None:
    workspace = await Workspace.open(tmp_path)
    await workspace.write_bytes("app.py", b"def run(value):\n    return eval(value)\n")
    await workspace.write_bytes(
        "rules.yml",
        b"""rules:
  - id: eval-input
    languages: [python]
    severity: WARNING
    message: Review dynamic evaluation of input
    pattern: eval($X)
""",
    )
    result = await DockerRunner(image="agentos/research:latest").run(
        command_for("semgrep", ".", "rules.yml"),
        workspace=workspace,
        timeout_seconds=60,
        max_output_chars=100000,
    )
    assert result.exit_code == 0, result.stderr
    report = parse_output("semgrep", result.stdout, result.exit_code)
    assert report["total_leads"] == 1
    assert report["leads"][0]["path"] == "app.py"
