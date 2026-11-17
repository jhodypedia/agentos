"""Scoped research workflow. Nothing in this service contacts a remote target."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    UnprocessableError,
)
from app.db.enums import FindingStatus
from app.db.models import Evidence, Finding, FindingReview, ResearchScope, SecurityScan
from app.memory.sensitivity import inspect
from app.schemas.research import EvidenceCreate, FindingCreate, ScanCreate, ScopeCreate
from app.security_research.scanners import BINARIES, command_for, parse_output
from app.services import workspaces
from app.workspace.paths import Workspace
from app.workspace.sandbox import DockerRunner, docker_available, docker_image_available

logger = structlog.get_logger("agentos.research")


async def get_scope(db: AsyncSession, scope_id: UUID, *, lock: bool = False) -> ResearchScope:
    query = select(ResearchScope).where(ResearchScope.id == scope_id)
    if lock:
        query = query.with_for_update()
    scope = (await db.execute(query)).scalar_one_or_none()
    if scope is None:
        raise NotFoundError("Research scope not found")
    return scope


def require_authorization(scope: ResearchScope) -> None:
    if scope.revoked_at or not scope.verified_at or not scope.verified_by:
        raise PermissionDeniedError("Scope authorization is not verified or has been revoked")
    if scope.expires_at and scope.expires_at <= datetime.now(UTC):
        raise PermissionDeniedError("Scope authorization has expired")


async def scope_workspace(db: AsyncSession, scope: ResearchScope) -> Workspace:
    project = await workspaces.get_project(db, scope.project_id)
    workspace = await workspaces.open_workspace(project)
    root = await workspace.resolve(scope.local_path, must_exist=True)
    if not await asyncio.to_thread(root.is_dir):
        raise UnprocessableError("Scope root must be a directory")
    return Workspace(root=root)


async def create_scope(db: AsyncSession, payload: ScopeCreate) -> ResearchScope:
    project = await workspaces.get_project(db, payload.project_id)
    workspace = await workspaces.open_workspace(project)
    root = await workspace.resolve(payload.local_path, must_exist=True)
    if not await asyncio.to_thread(root.is_dir):
        raise UnprocessableError("Scope root must be a directory")
    scoped = Workspace(root=root)
    excluded = []
    for path in payload.excluded_paths:
        excluded.append(scoped.relative(await scoped.resolve(path)))
    if payload.expires_at and (
        payload.expires_at.tzinfo is None or payload.expires_at <= datetime.now(UTC)
    ):
        raise UnprocessableError("Expiry must be a future timezone-aware timestamp")
    scope = ResearchScope(**payload.model_dump(exclude={"excluded_paths"}), excluded_paths=excluded)
    db.add(scope)
    await db.flush()
    return scope


async def get_finding(db: AsyncSession, finding_id: UUID, *, lock: bool = False) -> Finding:
    query = select(Finding).where(Finding.id == finding_id)
    if lock:
        query = query.with_for_update()
    finding = (await db.execute(query)).scalar_one_or_none()
    if finding is None:
        raise NotFoundError("Finding not found")
    return finding


async def create_finding(db: AsyncSession, payload: FindingCreate, actor: UUID | None) -> Finding:
    scope = await get_scope(db, payload.scope_id)
    require_authorization(scope)
    finding = Finding(**payload.model_dump(), project_id=scope.project_id, created_by=actor)
    db.add(finding)
    await db.flush()
    logger.info("finding_created", finding_id=str(finding.id), scope_id=str(scope.id))
    return finding


async def evidence_for(db: AsyncSession, finding_id: UUID) -> list[Evidence]:
    return list(
        (
            await db.scalars(
                select(Evidence)
                .where(Evidence.finding_id == finding_id)
                .order_by(Evidence.created_at, Evidence.id)
            )
        ).all()
    )


def evidence_digest(finding: Finding, evidence: list[Evidence]) -> str:
    data = {
        "title": finding.title,
        "hypothesis": finding.hypothesis,
        "severity": finding.severity.value,
        "description": finding.description,
        "cvss": str(finding.cvss_score),
        "cwe": finding.cwe_id,
        "remediation": finding.remediation,
        "evidence": sorted((str(e.id), e.content_hash) for e in evidence),
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


async def add_evidence(
    db: AsyncSession, finding: Finding, payload: EvidenceCreate, actor: UUID | None
) -> Evidence:
    if finding.status not in {FindingStatus.HYPOTHESIS, FindingStatus.VALIDATING}:
        raise ConflictError("Evidence is immutable after disposition; create a new hypothesis")
    if inspect("\n".join((payload.content, payload.title, payload.description))):
        raise UnprocessableError("Evidence contains credentials; redact them before recording")
    evidence = Evidence(
        finding_id=finding.id,
        **payload.model_dump(),
        content_hash=hashlib.sha256(payload.content.encode()).hexdigest(),
        meta={"recorded_by": str(actor) if actor else None},
    )
    db.add(evidence)
    await db.flush()
    return evidence


async def review_finding(
    db: AsyncSession, finding: Finding, *, actor: UUID, approved: bool, rationale: str
) -> FindingReview:
    if finding.status != FindingStatus.VALIDATING:
        raise ConflictError("Only validating findings can be reviewed")
    if finding.created_by is None or actor == finding.created_by:
        raise PermissionDeniedError("Review must be independent of the finding author")
    evidence = await evidence_for(db, finding.id)
    if approved and not any(e.kind == "validation" for e in evidence):
        raise ConflictError("Approval requires validation evidence, not scanner output alone")
    review = FindingReview(
        finding_id=finding.id,
        reviewer_id=actor,
        approved=approved,
        rationale=rationale,
        evidence_hash=evidence_digest(finding, evidence),
    )
    db.add(review)
    await db.flush()
    return review


async def transition(
    db: AsyncSession, finding: Finding, target: FindingStatus, rationale: str
) -> Finding:
    allowed = {
        FindingStatus.HYPOTHESIS: {FindingStatus.VALIDATING, FindingStatus.FALSE_POSITIVE},
        FindingStatus.VALIDATING: {FindingStatus.CONFIRMED, FindingStatus.FALSE_POSITIVE},
        FindingStatus.CONFIRMED: {FindingStatus.REPORTED},
        FindingStatus.REPORTED: {FindingStatus.ACCEPTED, FindingStatus.REJECTED},
    }
    if target not in allowed.get(finding.status, set()):
        raise ConflictError(f"Cannot move {finding.status.value} to {target.value}")
    if target == FindingStatus.REPORTED:
        raise ConflictError(
            "External reporting is not implemented; generating a report does not send it"
        )
    if target == FindingStatus.CONFIRMED:
        if finding.scope_id is None:
            raise ConflictError("Finding has no scope")
        require_authorization(await get_scope(db, finding.scope_id))
        evidence = await evidence_for(db, finding.id)
        latest = (
            await db.scalars(
                select(FindingReview)
                .where(FindingReview.finding_id == finding.id)
                .order_by(FindingReview.created_at.desc(), FindingReview.id.desc())
                .limit(1)
            )
        ).first()
        if (
            not any(e.kind == "validation" for e in evidence)
            or latest is None
            or not latest.approved
            or latest.evidence_hash != evidence_digest(finding, evidence)
        ):
            raise ConflictError(
                "Confirmation requires a current independent review of validation evidence"
            )
    finding.status = target
    finding.meta = {**(finding.meta or {}), "disposition_rationale": rationale}
    await db.flush()
    return finding


async def report(db: AsyncSession, finding: Finding) -> dict[str, Any]:
    evidence = await evidence_for(db, finding.id)
    lines = [
        f"# {finding.title}",
        "",
        f"Status: {finding.status.value}",
        f"Severity: {finding.severity.value}",
        f"CVSS: {finding.cvss_score}",
        f"CWE: {finding.cwe_id or 'unassigned'}",
        "",
        "## Hypothesis",
        finding.hypothesis,
        "",
        "## Description",
        finding.description,
        "",
        "## Remediation",
        finding.remediation,
        "",
        "## Evidence",
    ]
    for item in evidence:
        lines.extend(
            [
                f"### {item.title} ({item.kind})",
                f"SHA-256: {item.content_hash}",
                item.description,
                item.content,
                "",
            ]
        )
    return {
        "finding_id": str(finding.id),
        "status": finding.status.value,
        "evidence_hash": evidence_digest(finding, evidence),
        "markdown": "\n".join(lines),
        "externally_submitted": False,
    }


async def scanner_inventory() -> list[dict[str, Any]]:
    """Availability is checked inside the exact image that runs scans."""
    settings = get_settings()
    ready = await docker_available() and await docker_image_available(
        settings.security_sandbox_image
    )
    items = []
    for name, binary in BINARIES.items():
        available = False
        if ready:
            with tempfile.TemporaryDirectory(prefix="agentos-scanner-probe-") as temporary:
                probe = await DockerRunner(image=settings.security_sandbox_image).run(
                    f"command -v {binary}",
                    workspace=Workspace(root=Path(temporary)),
                    timeout_seconds=10,
                )
                available = probe.ok
        items.append(
            {
                "name": name,
                "available": available and name != "pip-audit",
                "sandbox": "docker",
                "network": "none",
                "reason": "Requires advisory network access; disabled in offline sandbox"
                if name == "pip-audit"
                else ""
                if available
                else "Scanner binary or network-disabled sandbox is unavailable",
            }
        )
    return items


async def run_scan(db: AsyncSession, payload: ScanCreate, actor: UUID | None) -> SecurityScan:
    scope = await get_scope(db, payload.scope_id)
    require_authorization(scope)
    if payload.tool not in scope.allowed_tools:
        raise PermissionDeniedError("Scanner is not permitted by this scope")
    if payload.tool == "pip-audit":
        raise UnprocessableError(
            "pip-audit requires an advisory service; network access is disabled"
        )
    workspace = await scope_workspace(db, scope)
    target = await workspace.resolve(payload.target_path, must_exist=True)
    # Reject excluded subtrees in either direction, so scanning '.' cannot
    # silently include an excluded descendant. Narrow the target instead.
    for excluded in scope.excluded_paths:
        path = await workspace.resolve(excluded)
        if target == path or target in path.parents or path in target.parents:
            raise PermissionDeniedError("Scan target overlaps an excluded path")
    config = None
    if payload.config_path:
        config_file = await workspace.resolve(payload.config_path, must_exist=True)
        config = workspace.relative(config_file)

    # Refuse symlinks anywhere beneath the target before handing it to a CLI.
    def check_tree() -> None:
        if target.is_symlink():
            raise PermissionDeniedError("Scanner targets must not contain symlinks")
        count = 0
        for root, dirs, files in os.walk(target, followlinks=False):
            for name in dirs + files:
                count += 1
                if count > 20000:
                    raise UnprocessableError("Scanner target exceeds 20000 filesystem entries")
                if (Path(root) / name).is_symlink():
                    raise PermissionDeniedError("Scanner targets must not contain symlinks")

    await asyncio.to_thread(check_tree)
    settings = get_settings()
    if not await docker_available() or not await docker_image_available(
        settings.security_sandbox_image
    ):
        from app.core.exceptions import ServiceUnavailableError

        raise ServiceUnavailableError("Security scans require a network-disabled Docker sandbox")
    command = command_for(payload.tool, workspace.relative(target) or ".", config)
    scan = SecurityScan(
        scope_id=scope.id,
        requested_by=actor,
        tool=payload.tool,
        target_path=payload.target_path,
        command=command,
    )
    db.add(scan)
    await db.flush()
    try:
        result = await DockerRunner(image=settings.security_sandbox_image).run(
            command,
            workspace=workspace,
            timeout_seconds=payload.timeout_seconds,
            max_output_chars=500000,
        )
        scan.exit_code = result.exit_code
        if result.timed_out or result.truncated:
            raise UnprocessableError("Scanner timed out or exceeded its output limit")
        scan.output = parse_output(payload.tool, result.stdout, result.exit_code)
        scan.status = "completed"
    except (OSError, UnprocessableError) as exc:
        scan.status = "failed"
        scan.error = str(exc)[:1000]
    scan.finished_at = datetime.now(UTC)
    await db.flush()
    logger.info("security_scan_finished", scan_id=str(scan.id), status=scan.status)
    return scan


async def import_scan(db: AsyncSession, scan: SecurityScan, actor: UUID) -> list[Finding]:
    """Import normalized leads idempotently, always at hypothesis status."""
    if scan.status != "completed":
        raise ConflictError("Only completed scans can be imported")
    scope = await get_scope(db, scan.scope_id)
    require_authorization(scope)
    findings = []
    for index, lead in enumerate(scan.output.get("leads", [])):
        key = f"{scan.id}:{index}"
        existing = (
            await db.scalars(select(Finding).where(Finding.meta["scanner_lead"].astext == key))
        ).first()
        if existing:
            findings.append(existing)
            continue
        finding = await create_finding(
            db,
            FindingCreate(
                scope_id=scope.id,
                title=str(lead.get("rule") or "Scanner lead")[:400],
                hypothesis="Investigate scanner lead: " + str(lead.get("message", ""))[:19000],
                description=json.dumps(lead),
                confidence=0.25,
            ),
            actor,
        )
        finding.meta = {"scanner_lead": key, "scan_id": str(scan.id)}
        await add_evidence(
            db,
            finding,
            EvidenceCreate(kind="scanner", title=f"{scan.tool} lead", content=json.dumps(lead)),
            actor,
        )
        findings.append(finding)
    await db.flush()
    return findings
