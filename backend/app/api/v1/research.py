"""Research records and local scans. Authorization verification is admin-only."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Query, Request
from sqlalchemy import case, select

from app.api.deps import AdminPrincipal, DbSession, OperatorPrincipal, ViewerPrincipal
from app.core.exceptions import ConflictError, NotFoundError
from app.db.enums import FindingStatus
from app.db.models import Finding, FindingReview, ResearchScope, SecurityScan
from app.schemas.research import (
    EvidenceCreate,
    EvidencePublic,
    FindingCreate,
    FindingPublic,
    FindingTransition,
    ReviewCreate,
    ReviewPublic,
    ScanCreate,
    ScanPublic,
    ScopeCreate,
    ScopePublic,
)
from app.services import research
from app.services.audit import record_audit

router = APIRouter(prefix="/research", tags=["security research"])


async def audit(
    db: DbSession, request: Request, actor: UUID, action: str, resource_id: UUID
) -> None:
    await record_audit(
        db,
        request=request,
        actor_user_id=actor,
        action=action,
        resource_type="research",
        resource_id=resource_id,
    )
    await db.commit()


@router.get("/scanners")
async def scanners(principal: ViewerPrincipal) -> list[dict[str, Any]]:
    return await research.scanner_inventory()


@router.get("/scopes", response_model=list[ScopePublic])
async def scopes(
    principal: ViewerPrincipal,
    db: DbSession,
    project_id: UUID | None = None,
    limit: int = Query(50, ge=1, le=200),
) -> list[ResearchScope]:
    query = select(ResearchScope).order_by(ResearchScope.created_at.desc()).limit(limit)
    if project_id:
        query = query.where(ResearchScope.project_id == project_id)
    return list((await db.scalars(query)).all())


@router.post("/scopes", response_model=ScopePublic, status_code=201)
async def create_scope(
    payload: ScopeCreate, principal: OperatorPrincipal, db: DbSession, request: Request
) -> ResearchScope:
    scope = await research.create_scope(db, payload)
    await audit(db, request, principal.user.id, "research.scope.created", scope.id)
    return scope


@router.post("/scopes/{scope_id}/verify", response_model=ScopePublic)
async def verify_scope(
    scope_id: UUID, principal: AdminPrincipal, db: DbSession, request: Request
) -> ResearchScope:
    scope = await research.get_scope(db, scope_id, lock=True)
    if scope.revoked_at:
        raise ConflictError("Revoked scope cannot be verified; register a new scope")
    if scope.expires_at and scope.expires_at <= datetime.now(UTC):
        raise ConflictError("Expired scope cannot be verified")
    await research.scope_workspace(db, scope)
    scope.verified_at = datetime.now(UTC)
    scope.verified_by = principal.user.id
    await audit(db, request, principal.user.id, "research.scope.verified", scope.id)
    return scope


@router.post("/scopes/{scope_id}/revoke", response_model=ScopePublic)
async def revoke_scope(
    scope_id: UUID, principal: AdminPrincipal, db: DbSession, request: Request
) -> ResearchScope:
    scope = await research.get_scope(db, scope_id, lock=True)
    scope.revoked_at = datetime.now(UTC)
    await audit(db, request, principal.user.id, "research.scope.revoked", scope.id)
    return scope


@router.get("/findings", response_model=list[FindingPublic])
async def findings(
    principal: ViewerPrincipal,
    db: DbSession,
    project_id: UUID | None = None,
    status: FindingStatus | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[Finding]:
    severity_order = case(
        {"critical": 5, "high": 4, "medium": 3, "low": 2, "info": 1},
        value=Finding.severity,
        else_=0,
    )
    query = select(Finding).order_by(
        severity_order.desc(), Finding.confidence.desc(), Finding.created_at.desc()
    )
    if project_id:
        query = query.where(Finding.project_id == project_id)
    if status:
        query = query.where(Finding.status == status)
    return list((await db.scalars(query.limit(limit).offset(offset))).all())


@router.post("/findings", response_model=FindingPublic, status_code=201)
async def create_finding(
    payload: FindingCreate, principal: OperatorPrincipal, db: DbSession, request: Request
) -> Finding:
    finding = await research.create_finding(db, payload, principal.user.id)
    await audit(db, request, principal.user.id, "research.finding.created", finding.id)
    return finding


@router.get("/findings/{finding_id}", response_model=FindingPublic)
async def get_finding(finding_id: UUID, principal: ViewerPrincipal, db: DbSession) -> Finding:
    return await research.get_finding(db, finding_id)


@router.get("/findings/{finding_id}/evidence", response_model=list[EvidencePublic])
async def evidence(finding_id: UUID, principal: ViewerPrincipal, db: DbSession) -> list[Any]:
    await research.get_finding(db, finding_id)
    return await research.evidence_for(db, finding_id)


@router.post("/findings/{finding_id}/evidence", response_model=EvidencePublic, status_code=201)
async def add_evidence(
    finding_id: UUID,
    payload: EvidenceCreate,
    principal: OperatorPrincipal,
    db: DbSession,
    request: Request,
) -> Any:
    finding = await research.get_finding(db, finding_id, lock=True)
    result = await research.add_evidence(db, finding, payload, principal.user.id)
    await audit(db, request, principal.user.id, "research.evidence.created", result.id)
    return result


@router.post("/findings/{finding_id}/status", response_model=FindingPublic)
async def transition(
    finding_id: UUID,
    payload: FindingTransition,
    principal: OperatorPrincipal,
    db: DbSession,
    request: Request,
) -> Finding:
    finding = await research.get_finding(db, finding_id, lock=True)
    await research.transition(db, finding, payload.status, payload.rationale)
    await audit(db, request, principal.user.id, "research.finding.transitioned", finding.id)
    return finding


@router.get("/findings/{finding_id}/reviews", response_model=list[ReviewPublic])
async def reviews(
    finding_id: UUID, principal: ViewerPrincipal, db: DbSession
) -> list[FindingReview]:
    await research.get_finding(db, finding_id)
    return list(
        (
            await db.scalars(
                select(FindingReview)
                .where(FindingReview.finding_id == finding_id)
                .order_by(FindingReview.created_at.desc())
            )
        ).all()
    )


@router.post("/findings/{finding_id}/reviews", response_model=ReviewPublic, status_code=201)
async def review(
    finding_id: UUID,
    payload: ReviewCreate,
    principal: OperatorPrincipal,
    db: DbSession,
    request: Request,
) -> FindingReview:
    finding = await research.get_finding(db, finding_id, lock=True)
    result = await research.review_finding(
        db, finding, actor=principal.user.id, **payload.model_dump()
    )
    await audit(db, request, principal.user.id, "research.finding.reviewed", finding.id)
    return result


@router.get("/findings/{finding_id}/report")
async def report(finding_id: UUID, principal: ViewerPrincipal, db: DbSession) -> dict[str, Any]:
    return await research.report(db, await research.get_finding(db, finding_id))


@router.post("/scans", response_model=ScanPublic, status_code=201)
async def scan(
    payload: ScanCreate, principal: AdminPrincipal, db: DbSession, request: Request
) -> SecurityScan:
    result = await research.run_scan(db, payload, principal.user.id)
    await audit(db, request, principal.user.id, "research.scan.finished", result.id)
    return result


@router.get("/scans", response_model=list[ScanPublic])
async def scans(
    principal: ViewerPrincipal,
    db: DbSession,
    scope_id: UUID | None = None,
    limit: int = Query(50, ge=1, le=200),
) -> list[SecurityScan]:
    query = select(SecurityScan).order_by(SecurityScan.created_at.desc()).limit(limit)
    if scope_id:
        query = query.where(SecurityScan.scope_id == scope_id)
    return list((await db.scalars(query)).all())


@router.get("/scans/{scan_id}", response_model=ScanPublic)
async def get_scan(scan_id: UUID, principal: ViewerPrincipal, db: DbSession) -> SecurityScan:
    scan = await db.get(SecurityScan, scan_id)
    if scan is None:
        raise NotFoundError("Scan not found")
    return scan


@router.post("/scans/{scan_id}/hypotheses", response_model=list[FindingPublic])
async def import_scan(
    scan_id: UUID, principal: OperatorPrincipal, db: DbSession, request: Request
) -> list[Finding]:
    scan = (
        await db.scalars(select(SecurityScan).where(SecurityScan.id == scan_id).with_for_update())
    ).first()
    if scan is None:
        raise NotFoundError("Scan not found")
    findings = await research.import_scan(db, scan, principal.user.id)
    await audit(db, request, principal.user.id, "research.scan.imported", scan.id)
    return findings
