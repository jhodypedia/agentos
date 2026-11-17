"""Evidence integrity, independent review, scope enforcement and scanner failures."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.core.exceptions import PermissionDeniedError, UnprocessableError
from app.db.models import ResearchScope
from app.security_research.scanners import command_for, parse_output
from app.services.research import require_authorization


def test_scope_requires_human_verification() -> None:
    scope = ResearchScope()
    with pytest.raises(PermissionDeniedError):
        require_authorization(scope)
    scope.verified_at = datetime.now(UTC)
    scope.verified_by = uuid4()
    require_authorization(scope)
    scope.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(PermissionDeniedError, match="expired"):
        require_authorization(scope)
    scope.expires_at = None
    scope.revoked_at = datetime.now(UTC)
    with pytest.raises(PermissionDeniedError, match="revoked"):
        require_authorization(scope)


def test_gitleaks_never_preserves_secret_fields() -> None:
    output = parse_output(
        "gitleaks",
        json.dumps(
            [
                {
                    "RuleID": "token",
                    "File": "app.py",
                    "StartLine": 3,
                    "Secret": "sensitive-secret",
                    "Match": "sensitive-secret",
                    "Line": "sensitive-secret",
                }
            ]
        ),
        10,
    )
    assert "sensitive-secret" not in json.dumps(output)
    assert output["leads"][0]["line"] == 3


@pytest.mark.parametrize(
    "tool,code,output",
    [
        ("semgrep", 2, '{"results": []}'),
        ("semgrep", 0, '{"results": [], "errors": ["parse error"]}'),
        ("slither", 0, '{"success": false}'),
        ("gitleaks", 1, "[]"),
        ("semgrep", 0, "truncated"),
    ],
)
def test_scanner_errors_are_not_clean_scans(tool: str, code: int, output: str) -> None:
    with pytest.raises(UnprocessableError):
        parse_output(tool, output, code)


def test_scanner_command_quotes_paths_and_requires_local_rules() -> None:
    import shlex

    argv = shlex.split(command_for("gitleaks", "./target; touch /tmp/evil"))
    assert argv[2] == "./target; touch /tmp/evil"
    with pytest.raises(UnprocessableError):
        command_for("semgrep", ".")


def test_semgrep_extracts_leads_without_claiming_confirmation() -> None:
    result = parse_output(
        "semgrep",
        json.dumps(
            {
                "results": [
                    {
                        "check_id": "eval",
                        "path": "app.py",
                        "start": {"line": 2},
                        "extra": {"message": "User input reaches eval"},
                    }
                ],
                "errors": [],
            }
        ),
        0,
    )
    assert result["total_leads"] == 1
    assert "confirmed" not in result


def test_scanner_message_redaction_preserves_json_structure() -> None:
    result = parse_output(
        "semgrep",
        json.dumps(
            {
                "results": [
                    {
                        "check_id": "secret",
                        "extra": {"message": "api_key=secret-value"},
                    }
                ],
                "errors": [],
            }
        ),
        0,
    )
    assert "secret-value" not in json.dumps(result)
    assert result["total_leads"] == 1
