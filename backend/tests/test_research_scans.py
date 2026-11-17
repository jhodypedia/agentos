from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from app.core.exceptions import PermissionDeniedError
from app.db.enums import FindingStatus, UserRole
from app.db.models import ResearchScope, SecurityScan
from app.db.session import get_session_factory
from app.schemas.research import ScanCreate
from app.services import research
from app.services.research_tools import research_resources
from app.services.users import create_user
from app.services.workspaces import create_project
from app.workspace.sandbox import CommandResult

pytestmark = pytest.mark.integration


async def test_scan_import_is_idempotent_and_always_hypothesis(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from datetime import UTC, datetime

    from app.core.config import get_settings

    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    get_settings.cache_clear()

    async def available(*args: object) -> bool:
        return True

    async def run(*args: object, **kwargs: object) -> CommandResult:
        return CommandResult(
            command="semgrep",
            exit_code=0,
            stdout='{"results":[{"check_id":"eval-input","path":"app.py",'
            '"extra":{"message":"Possible code injection"}}],"errors":[]}',
            stderr="",
            duration_ms=1,
            sandbox="docker",
        )

    monkeypatch.setattr(research, "docker_available", available)
    monkeypatch.setattr(research, "docker_image_available", available)
    monkeypatch.setattr(research.DockerRunner, "run", run)
    try:
        async with get_session_factory()() as db:
            user = await create_user(
                db, email="scanner@example.com", password="Str0ng-Passw0rd!", role=UserRole.ADMIN
            )
            project = await create_project(db, slug="scanner-test", name="Scan", owner_id=user.id)
            root = Path(project.workspace_path)
            scope = ResearchScope(
                project_id=project.id,
                name="Local",
                authorization="Owner consent",
                local_path=".",
                allowed_tools=["semgrep"],
                verified_by=user.id,
                verified_at=datetime.now(UTC),
                excluded_paths=[],
                external_targets=[],
            )
            db.add(scope)
            await db.flush()
            # Existing workspace API is used to write fixtures.
            from app.workspace.paths import Workspace

            workspace = await Workspace.open(root)
            await workspace.write_bytes("rules.yml", b"rules: []")
            scan = await research.run_scan(
                db, ScanCreate(scope_id=scope.id, tool="semgrep", config_path="rules.yml"), user.id
            )
            assert scan.status == "completed"
            first = await research.import_scan(db, scan, user.id)
            second = await research.import_scan(db, scan, user.id)
            assert len(first) == 1
            assert first[0].id == second[0].id
            assert first[0].status == FindingStatus.HYPOTHESIS
            assert (await research.evidence_for(db, first[0].id))[0].kind == "scanner"
            await db.commit()
            resources = research_resources(
                get_session_factory(), project_id=uuid4(), user_id=user.id
            )
            with pytest.raises(PermissionDeniedError):
                await resources["research_report"](str(first[0].id))
    finally:
        get_settings.cache_clear()


def test_failed_scan_has_no_leads_to_confirm() -> None:
    scan = SecurityScan(status="failed", output={}, error="Scanner unavailable")
    assert not scan.output.get("leads")
