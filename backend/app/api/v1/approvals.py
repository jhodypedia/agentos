"""Approval endpoints: list, inspect and decide on human sign-off requests."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Query, Request, status

from app.api.deps import DbSession, OperatorPrincipal
from app.db.enums import ApprovalStatus
from app.schemas.approval import (
    ApprovalCreate,
    ApprovalDecision,
    ApprovalPage,
    ApprovalPublic,
)
from app.services import approvals as approval_service
from app.services.audit import record_audit

router = APIRouter(prefix="/approvals", tags=["approvals"])


@router.get("", response_model=ApprovalPage, summary="List approvals")
async def list_approvals(
    principal: OperatorPrincipal,
    db: DbSession,
    approval_status: ApprovalStatus | None = Query(default=None, alias="status"),
    task_id: UUID | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> ApprovalPage:
    items, total = await approval_service.list_approvals(
        db, status=approval_status, task_id=task_id, limit=limit, offset=offset
    )
    return ApprovalPage(
        total=total,
        limit=limit,
        offset=offset,
        items=[ApprovalPublic.model_validate(item) for item in items],
    )


@router.post(
    "",
    response_model=ApprovalPublic,
    status_code=status.HTTP_201_CREATED,
    summary="Request an approval",
)
async def create_approval(
    payload: ApprovalCreate,
    principal: OperatorPrincipal,
    db: DbSession,
    request: Request,
) -> ApprovalPublic:
    approval = await approval_service.create_approval(
        db,
        title=payload.title,
        description=payload.description,
        action_type=payload.action_type,
        task_id=payload.task_id,
        step_id=payload.step_id,
        requested_by_agent_id=None,
        assigned_to_user_id=None,
        payload=payload.payload,
        ttl_hours=payload.ttl_hours,
    )
    await record_audit(
        db,
        action="approval.requested",
        resource_type="approval",
        resource_id=approval.id,
        actor_user_id=principal.user.id,
        request=request,
        meta={"task_id": str(payload.task_id) if payload.task_id else None},
    )
    await db.commit()
    await db.refresh(approval)
    return ApprovalPublic.model_validate(approval)


@router.get("/{approval_id}", response_model=ApprovalPublic, summary="Get one approval")
async def get_approval(
    approval_id: UUID, principal: OperatorPrincipal, db: DbSession
) -> ApprovalPublic:
    return ApprovalPublic.model_validate(await approval_service.get_approval(db, approval_id))


@router.post(
    "/{approval_id}/decide",
    response_model=ApprovalPublic,
    summary="Approve or reject a pending request",
)
async def decide_approval(
    approval_id: UUID,
    payload: ApprovalDecision,
    principal: OperatorPrincipal,
    db: DbSession,
    request: Request,
) -> ApprovalPublic:
    bus = getattr(request.app.state, "event_bus", None)
    approval = await approval_service.decide(
        db,
        approval_id=approval_id,
        approved=payload.approved,
        decided_by_user_id=principal.user.id,
        note=payload.note,
        bus=bus,
    )
    await record_audit(
        db,
        action="approval.approved" if payload.approved else "approval.rejected",
        resource_type="approval",
        resource_id=approval.id,
        actor_user_id=principal.user.id,
        request=request,
        meta={"task_id": str(approval.task_id) if approval.task_id else None},
    )
    await db.commit()
    await db.refresh(approval)
    return ApprovalPublic.model_validate(approval)
