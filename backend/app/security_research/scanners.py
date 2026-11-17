"""Fixed scanner commands and strict output parsing.

Execution uses the existing network-disabled Docker runner, never its host
fallback. Repository build hooks and test code therefore execute in isolation.
"""

from __future__ import annotations

import json
import shlex
from typing import Any

from app.core.exceptions import UnprocessableError

BINARIES = {
    "semgrep": "semgrep",
    "gitleaks": "gitleaks",
    "slither": "slither",
    "forge": "forge",
    "echidna": "echidna",
    "pip-audit": "pip-audit",
}


def command_for(tool: str, target: str, config: str | None = None) -> str:
    """Build argv from a closed set of options, then quote for the runner."""
    target = "./" + target.removeprefix("./")
    if config:
        config = "./" + config.removeprefix("./")
    if tool == "semgrep":
        if not config:
            raise UnprocessableError("Semgrep requires a local rules file")
        argv = [
            "semgrep",
            "scan",
            "--config",
            config,
            "--json",
            "--metrics=off",
            "--disable-version-check",
            "--jobs=1",
            "--max-memory=512",
            target,
        ]
    elif tool == "gitleaks":
        argv = [
            "gitleaks",
            "dir",
            target,
            "--redact=100",
            "--no-banner",
            "--report-format=json",
            "--report-path=/dev/stdout",
            "--exit-code=10",
        ]
    elif tool == "slither":
        argv = ["slither", target, "--json", "-"]
    elif tool == "forge":
        argv = ["forge", "test", "--root", target, "--json", "--offline"]
    elif tool == "echidna":
        argv = ["echidna", target, "--format", "json", "--test-limit", "10000"]
    elif tool == "pip-audit":
        argv = [
            "pip-audit",
            "--no-deps",
            "--disable-pip",
            "--require-hashes",
            "--requirement",
            target,
            "--format=json",
            "--progress-spinner=off",
        ]
    else:
        raise UnprocessableError("Unknown scanner")
    return shlex.join(argv)


def parse_output(tool: str, stdout: str, exit_code: int) -> dict[str, Any]:
    """Reject failed/malformed reports rather than interpreting them as clean."""
    allowed = {
        "gitleaks": {0, 10},
        "semgrep": {0, 1},
        "slither": {0, 255},
        "forge": {0, 1},
        "echidna": {0, 1},
        "pip-audit": {0, 1},
    }
    if exit_code not in allowed.get(tool, {0}):
        raise UnprocessableError(f"{tool} failed with exit code {exit_code}")
    try:
        data = json.loads(stdout)
    except (ValueError, TypeError) as exc:
        raise UnprocessableError(f"{tool} did not return a complete JSON report") from exc
    # Scanner messages and test logs may contain source snippets and credentials.
    from app.memory.sensitivity import redact

    def sanitized(value: Any) -> Any:
        if isinstance(value, str):
            return redact(value)
        if isinstance(value, list):
            return [sanitized(item) for item in value]
        if isinstance(value, dict):
            return {key: sanitized(item) for key, item in value.items()}
        return value

    data = sanitized(data)
    leads: list[dict[str, Any]] = []
    if tool == "semgrep":
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise UnprocessableError("Invalid Semgrep report")
        if data.get("errors"):
            raise UnprocessableError("Semgrep reported analysis errors; scan is incomplete")
        for row in data["results"]:
            leads.append(
                {
                    "rule": row.get("check_id"),
                    "path": row.get("path"),
                    "line": row.get("start", {}).get("line"),
                    "message": row.get("extra", {}).get("message", "Scanner lead"),
                }
            )
    elif tool == "gitleaks":
        if not isinstance(data, list):
            raise UnprocessableError("Invalid Gitleaks report")
        # Deliberately omit Secret, Match and Line even with --redact enabled.
        leads = [
            {
                "rule": r.get("RuleID"),
                "path": r.get("File"),
                "line": r.get("StartLine"),
                "message": "Potential exposed credential",
            }
            for r in data
        ]
    elif tool == "slither":
        if not isinstance(data, dict) or data.get("success") is not True:
            raise UnprocessableError("Slither compilation or analysis failed")
        leads = [
            {"rule": r.get("check"), "message": r.get("description", "Scanner lead")}
            for r in data.get("results", {}).get("detectors", [])
        ]
    elif tool == "pip-audit":
        if not isinstance(data, dict) or not isinstance(data.get("dependencies"), list):
            raise UnprocessableError("Invalid pip-audit report")
        for dep in data["dependencies"]:
            if dep.get("skip_reason"):
                raise UnprocessableError("Dependency audit skipped a dependency")
            for vuln in dep.get("vulns", []):
                leads.append(
                    {
                        "rule": vuln.get("id"),
                        "message": (
                            f"{dep.get('name')} {dep.get('version')}: {vuln.get('description', '')}"
                        ),
                    }
                )
    else:
        if not isinstance(data, dict):
            raise UnprocessableError(f"Invalid {tool} report")
        if tool == "echidna" and not isinstance(data.get("tests"), list):
            raise UnprocessableError("Invalid Echidna campaign report")
        # A failing test is validation output, not automatically a vulnerability.
        return {"leads": [], "test_report": data, "exit_code": exit_code}
    return {"leads": leads[:200], "total_leads": len(leads), "exit_code": exit_code}
