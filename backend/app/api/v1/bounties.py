"""Bounty endpoints: competition board, requirements and entry submission.

Reads are viewer-visible because a competition board is public information. Every
route that talks to make.fun on the platform's behalf (sync, requirements
refresh, quota, prepare, submit, status refresh) is operator-only and audited:
the operations cost rate limit, and a submission is externally visible and
irreversible.

Submitting is the one action with a hard precondition. ``POST /entries`` records
the payload and asks for approval; ``POST /entries/{id}/submit`` refuses unless
that approval was granted. There is no parameter that skips the check.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Query, Request, status
from sqlalchemy import select

from app.api.deps import DbSession, OperatorPrincipal, ViewerPrincipal
from app.bounty.makefun import MakeFunError, MakeFunProblem
from app.db.enums import CompetitionEntryStatus, CompetitionStatus
from app.db.models import BountyProgram, Competition
from app.schemas.bounty import (
    BountyProgramPublic,
    CompetitionDetail,
    CompetitionEntryPublic,
    CompetitionPublic,
    CompetitionSyncRequest,
    CompetitionSyncResult,
    EntryPrepare,
    QuotaPublic,
)
from app.services import bounty as bounty_service
from app.services.audit import record_audit

router = APIRouter(prefix="/bounties", tags=["bounties"])


@router.get("/programs", response_model=list[BountyProgramPublic], summary="List bounty programs")
async def list_programs(principal: ViewerPrincipal, db: DbSession) -> list[BountyProgramPublic]:
    result = await db.execute(select(BountyProgram).order_by(BountyProgram.name))
    return [BountyProgramPublic.model_validate(row) for row in result.scalars()]


@router.get(
    "/competitions", response_model=list[CompetitionPublic], summary="List known competitions"
)
async def list_competitions(
    principal: ViewerPrincipal,
    db: DbSession,
    competition_status: CompetitionStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[CompetitionPublic]:
    items, _ = await bounty_service.list_competitions(
        db, status=competition_status, limit=limit, offset=offset
    )
    return [CompetitionPublic.model_validate(item) for item in items]


@router.post(
    "/competitions/sync",
    response_model=CompetitionSyncResult,
    summary="Refresh the competition board from make.fun",
)
async def sync_competitions(
    principal: OperatorPrincipal,
    db: DbSession,
    request: Request,
    payload: CompetitionSyncRequest | None = None,
) -> CompetitionSyncResult:
    options = payload or CompetitionSyncRequest()
    result = await bounty_service.sync_competitions(db, limit=options.limit, status=options.status)
    await record_audit(
        db,
        action="bounty.sync",
        resource_type="bounty_program",
        resource_id=bounty_service.PROGRAM_SLUG,
        actor_user_id=principal.user.id,
        request=request,
        meta={"created": len(result["created"]), "updated": len(result["updated"])},
    )
    await db.commit()
    return CompetitionSyncResult.model_validate(result)


@router.get(
    "/competitions/{competition_id}",
    response_model=CompetitionDetail,
    summary="Inspect a competition",
)
async def get_competition(
    competition_id: UUID, principal: ViewerPrincipal, db: DbSession
) -> CompetitionDetail:
    competition = await bounty_service.get_competition(db, competition_id)
    return _detail(competition)


@router.get(
    "/competitions/by-slug/{slug}",
    response_model=CompetitionDetail,
    summary="Inspect a competition by its make.fun slug",
)
async def get_competition_by_slug(
    slug: str, principal: ViewerPrincipal, db: DbSession
) -> CompetitionDetail:
    competition = await bounty_service.get_competition_by_slug(db, slug)
    return _detail(competition)


@router.post(
    "/competitions/{competition_id}/requirements/refresh",
    response_model=CompetitionDetail,
    summary="Fetch the published requirements for a competition",
)
async def refresh_requirements(
    competition_id: UUID, principal: OperatorPrincipal, db: DbSession, request: Request
) -> CompetitionDetail:
    competition = await bounty_service.get_competition(db, competition_id)
    requirements = await bounty_service.refresh_requirements(db, competition)
    await record_audit(
        db,
        action="bounty.requirements.refresh",
        resource_type="competition",
        resource_id=competition.id,
        actor_user_id=principal.user.id,
        request=request,
        meta={"checks": len(requirements.get("checks") or [])},
    )
    await db.commit()
    return _detail(competition)


@router.get(
    "/competitions/{competition_id}/quota",
    response_model=QuotaPublic,
    summary="Entries left before the platform starts refusing them",
)
async def get_quota(
    competition_id: UUID, principal: OperatorPrincipal, db: DbSession
) -> QuotaPublic:
    competition = await bounty_service.get_competition(db, competition_id)
    payload = await bounty_service.check_quota(competition)
    return QuotaPublic.model_validate(payload)


@router.get(
    "/competitions/{competition_id}/entries",
    response_model=list[CompetitionEntryPublic],
    summary="List entries for a competition",
)
async def list_competition_entries(
    competition_id: UUID, principal: ViewerPrincipal, db: DbSession
) -> list[CompetitionEntryPublic]:
    entries = await bounty_service.list_entries(db, competition_id=competition_id)
    return [CompetitionEntryPublic.model_validate(entry) for entry in entries]


@router.post(
    "/competitions/{competition_id}/entries",
    response_model=CompetitionEntryPublic,
    status_code=status.HTTP_201_CREATED,
    summary="Prepare an entry and request approval to submit it",
)
async def prepare_entry(
    competition_id: UUID,
    payload: EntryPrepare,
    principal: OperatorPrincipal,
    db: DbSession,
    request: Request,
) -> CompetitionEntryPublic:
    competition = await bounty_service.get_competition(db, competition_id)
    await bounty_service.ensure_project(db, competition, owner_id=principal.user.id)
    entry = await bounty_service.prepare_entry(
        db,
        competition=competition,
        repo_url=payload.repo_url,
        commit_sha=payload.commit_sha,
        title=payload.title,
        transcript_paths=payload.transcript_paths,
        created_by=principal.user.id,
    )
    await record_audit(
        db,
        action="bounty.entry.prepare",
        resource_type="competition_entry",
        resource_id=entry.id,
        actor_user_id=principal.user.id,
        request=request,
        meta={"competition": competition.external_id, "approval_id": str(entry.approval_id)},
    )
    await db.commit()
    return CompetitionEntryPublic.model_validate(entry)


@router.get(
    "/entries", response_model=list[CompetitionEntryPublic], summary="List competition entries"
)
async def list_entries(
    principal: ViewerPrincipal,
    db: DbSession,
    entry_status: CompetitionEntryStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[CompetitionEntryPublic]:
    entries = await bounty_service.list_entries(db, status=entry_status, limit=limit, offset=offset)
    return [CompetitionEntryPublic.model_validate(entry) for entry in entries]


@router.get(
    "/entries/{entry_id}", response_model=CompetitionEntryPublic, summary="Inspect an entry"
)
async def get_entry(
    entry_id: UUID, principal: ViewerPrincipal, db: DbSession
) -> CompetitionEntryPublic:
    entry = await bounty_service.get_entry(db, entry_id)
    return CompetitionEntryPublic.model_validate(entry)


@router.post(
    "/entries/{entry_id}/submit",
    response_model=CompetitionEntryPublic,
    summary="Submit an approved entry to make.fun",
)
async def submit_entry(
    entry_id: UUID, principal: OperatorPrincipal, db: DbSession, request: Request
) -> CompetitionEntryPublic:
    entry = await bounty_service.get_entry(db, entry_id)
    try:
        entry = await bounty_service.submit_entry(db, entry)
    except MakeFunProblem as exc:
        await record_audit(
            db,
            action="bounty.entry.submit",
            resource_type="competition_entry",
            resource_id=entry.id,
            actor_user_id=principal.user.id,
            request=request,
            success=False,
            meta={
                "stage": exc.stage,
                "checks": [check.get("check") for check in exc.checks],
            },
        )
        await db.commit()
        raise
    except MakeFunError as exc:
        # Authentication or transport failure: nothing was submitted and the
        # entry keeps its state, so the operator can fix the cause and retry.
        await record_audit(
            db,
            action="bounty.entry.submit",
            resource_type="competition_entry",
            resource_id=entry.id,
            actor_user_id=principal.user.id,
            request=request,
            success=False,
            meta={"error": exc.message[:500]},
        )
        await db.commit()
        raise
    await record_audit(
        db,
        action="bounty.entry.submit",
        resource_type="competition_entry",
        resource_id=entry.id,
        actor_user_id=principal.user.id,
        request=request,
        meta={"submission_id": entry.submission_id},
    )
    await db.commit()
    return CompetitionEntryPublic.model_validate(entry)


@router.post(
    "/entries/{entry_id}/refresh",
    response_model=CompetitionEntryPublic,
    summary="Pull the platform's verdict for a submitted entry",
)
async def refresh_entry(
    entry_id: UUID, principal: OperatorPrincipal, db: DbSession
) -> CompetitionEntryPublic:
    entry = await bounty_service.get_entry(db, entry_id)
    entry = await bounty_service.refresh_entry_status(db, entry)
    await db.commit()
    return CompetitionEntryPublic.model_validate(entry)


def _detail(competition: Competition) -> CompetitionDetail:
    requirements = competition.requirements or {}
    return CompetitionDetail(
        **CompetitionPublic.model_validate(competition).model_dump(),
        requirements_summary=bounty_service.requirements_summary(requirements)
        if requirements
        else {},
    )
