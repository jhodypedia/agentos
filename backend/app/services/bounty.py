"""make.fun competitions: discovery, requirements and entry workflow.

The service keeps three things strictly apart, because merging them is how a
bounty integration starts lying to its operator:

1. **What the platform published.** ``sync_competitions`` stores exactly what
   ``GET /api/competitions`` returned. A listing is never inferred, and a stale
   row keeps its original ``synced_at`` so staleness is visible.
2. **What the rules require.** ``requirements`` are fetched per competition and
   stored verbatim, including the checks and their fixes. Deadline, quota and
   required deliverables are read from that document, not from the short board
   listing.
3. **What we did.** ``CompetitionEntry`` rows describe attempts: the repo commit,
   the transcript paths uploaded, the approval that authorised it and the
   verdict the platform returned.

Submission is deliberately split into prepare → approve → submit. Preparing
reserves the project workspace and records the exact payload; approving is a
human decision recorded in ``approvals``; submitting uses that approval and
nothing else. A caller cannot skip the middle step by omitting a flag.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bounty.makefun import MakeFunClient, MakeFunError, MakeFunProblem
from app.core.config import get_settings
from app.core.events import Event, EventType, RedisEventBus
from app.core.exceptions import ConflictError, NotFoundError, UnprocessableError
from app.db.enums import (
    ApprovalAction,
    ApprovalStatus,
    BountyPlatform,
    CompetitionEntryStatus,
    CompetitionStatus,
)
from app.db.models import Approval, BountyProgram, Competition, CompetitionEntry, Project
from app.services import approvals as approval_service
from app.services import workspaces as workspace_service

logger = structlog.get_logger("agentos.services.bounty")

PROGRAM_SLUG = "make-fun"
PROGRAM_NAME = "make.fun"


def makefun_client() -> MakeFunClient:
    settings = get_settings()
    key = settings.makefun_api_key.get_secret_value() if settings.makefun_api_key else None
    return MakeFunClient(
        base_url=settings.makefun_base_url,
        api_key=key,
        timeout_seconds=settings.makefun_timeout_seconds,
    )


def _deadline(raw: Any) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    with contextlib.suppress(ValueError):
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return None


def _money(raw: Any) -> Decimal | None:
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except (ArithmeticError, ValueError):
        return None


def _status_for(listing_status: str, deadline: datetime | None) -> CompetitionStatus:
    """Map the platform's status enum onto ours.

    make.fun answers ``draft | open | closed | resolved``. An open listing whose
    deadline has passed is stored as ``closed``: the API stops accepting entries
    then, so reporting it as open would be wrong even though the field says so.
    """
    normalised = (listing_status or "").lower()
    if normalised == "resolved":
        return CompetitionStatus.CLOSED
    if normalised == "closed":
        return CompetitionStatus.CLOSED
    if normalised == "draft":
        return CompetitionStatus.DISCOVERED
    if deadline is not None and deadline <= datetime.now(UTC):
        return CompetitionStatus.CLOSED
    return CompetitionStatus.DISCOVERED


async def get_program(db: AsyncSession) -> BountyProgram:
    """Return (creating if needed) the make.fun program row."""
    result = await db.execute(select(BountyProgram).where(BountyProgram.slug == PROGRAM_SLUG))
    program = result.scalar_one_or_none()
    if program is not None:
        return program
    program = BountyProgram(
        slug=PROGRAM_SLUG,
        name=PROGRAM_NAME,
        platform=BountyPlatform.MAKE_FUN,
        url=get_settings().makefun_base_url,
        description="Coding bounties with on-chain USDC rewards, entered by AI agents.",
        scope={"entry": "public repository plus coding-agent transcripts"},
    )
    db.add(program)
    await db.flush()
    return program


async def sync_competitions(
    db: AsyncSession,
    *,
    client: MakeFunClient | None = None,
    limit: int | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """Upsert the board into ``competitions``.

    Returns counts plus the slugs touched, so an operator can see what a sync
    actually did instead of trusting a boolean.
    """
    settings = get_settings()
    program = await get_program(db)
    own_client = client is None
    api = client or makefun_client()
    try:
        board = await api.list_competitions(
            limit=limit or settings.makefun_sync_limit, status=status
        )
    finally:
        if own_client:
            await api.aclose()

    items = board.get("items") or []
    if not isinstance(items, list):
        raise MakeFunError("make.fun returned an unexpected board payload")

    created: list[str] = []
    updated: list[str] = []
    synced_at = datetime.now(UTC)

    for listing in items:
        if not isinstance(listing, dict):
            continue
        external_id = str(listing.get("slug") or listing.get("id") or "").strip()
        if not external_id:
            continue
        deadline = _deadline(listing.get("deadline"))
        reward = _money(listing.get("rewardAmount"))
        values = {
            "title": str(listing.get("title") or external_id)[:300],
            "description": str(listing.get("description") or ""),
            "url": f"{settings.makefun_base_url}{listing.get('url') or ''}"
            if listing.get("url")
            else "",
            "reward_amount": reward,
            "reward_currency": "USD",
            "status": _status_for(str(listing.get("status") or ""), deadline),
            "deadline_at": deadline,
            "starts_at": _deadline(listing.get("publishedAt")),
            "scoring": {
                "rewards": listing.get("rewards"),
                "entry_fee_usd": listing.get("entryFeeUsd"),
                "pot_usd": listing.get("potUsd"),
            },
            "meta": {
                "sponsor": listing.get("sponsor"),
                "category": listing.get("category"),
                "skills": listing.get("skills"),
                "submission_count": listing.get("submissionCount"),
                "listing_status": listing.get("status"),
                "synced_at": synced_at.isoformat(),
            },
        }

        result = await db.execute(
            select(Competition).where(
                Competition.program_id == program.id, Competition.external_id == external_id
            )
        )
        competition = result.scalar_one_or_none()
        if competition is None:
            db.add(Competition(program_id=program.id, external_id=external_id, **values))
            created.append(external_id)
        else:
            for key, value in values.items():
                setattr(competition, key, value)
            updated.append(external_id)

    await db.flush()
    logger.info("makefun_sync", created=len(created), updated=len(updated))
    return {
        "created": created,
        "updated": updated,
        "total": len(items),
        "next_cursor": board.get("next_cursor"),
        "synced_at": synced_at.isoformat(),
    }


async def list_competitions(
    db: AsyncSession,
    *,
    status: CompetitionStatus | None = None,
    program_id: UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Competition], int]:
    base = select(Competition)
    if status is not None:
        base = base.where(Competition.status == status)
    if program_id is not None:
        base = base.where(Competition.program_id == program_id)

    total = await db.scalar(select(func.count()).select_from(base.subquery())) or 0
    # Live competitions come first. Sorting purely by deadline would fill the
    # first page with listings that are already closed, which is the one thing
    # an operator scanning the board does not want to see.
    closed = Competition.status.in_([CompetitionStatus.CLOSED, CompetitionStatus.ARCHIVED])
    result = await db.execute(
        base.order_by(
            closed.asc(),
            Competition.deadline_at.is_(None),
            Competition.deadline_at.asc(),
            Competition.created_at.desc(),
        )
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars()), int(total)


async def get_competition(db: AsyncSession, competition_id: UUID) -> Competition:
    competition = await db.get(Competition, competition_id)
    if competition is None:
        raise NotFoundError("Competition not found")
    return competition


async def get_competition_by_slug(db: AsyncSession, slug: str) -> Competition:
    result = await db.execute(select(Competition).where(Competition.external_id == slug))
    competition = result.scalar_one_or_none()
    if competition is None:
        raise NotFoundError(f"Competition '{slug}' has not been synced")
    return competition


async def refresh_requirements(
    db: AsyncSession,
    competition: Competition,
    *,
    client: MakeFunClient | None = None,
) -> dict[str, Any]:
    """Fetch and store the published requirements for one competition."""
    own_client = client is None
    api = client or makefun_client()
    try:
        requirements = await api.get_requirements(competition.external_id)
    finally:
        if own_client:
            await api.aclose()

    if not isinstance(requirements, dict):
        raise MakeFunError("make.fun returned an unexpected requirements payload")

    deadlines = requirements.get("deadlines") or {}
    entries_close = _deadline(deadlines.get("entries_close"))
    if entries_close is not None:
        competition.deadline_at = entries_close

    competition.requirements = requirements
    competition.status = _status_for(
        str((requirements.get("competition") or {}).get("status") or competition.status.value),
        competition.deadline_at,
    )
    await db.flush()
    logger.info(
        "makefun_requirements",
        slug=competition.external_id,
        checks=len(requirements.get("checks") or []),
    )
    return requirements


def requirements_summary(requirements: dict[str, Any]) -> dict[str, Any]:
    """The parts of a requirements document an operator actually acts on.

    make.fun's document is large (it carries a README template, CLI recipes and
    a DRand anchor). Summarising it is not cosmetic: an agent planning a build
    needs the deliverables, the checks and the deadlines, and a summary that
    dropped the checks would hide exactly the rules that reject entries.
    """
    checks = [c for c in (requirements.get("checks") or []) if isinstance(c, dict)]
    deliverables = [d for d in (requirements.get("deliverables") or []) if isinstance(d, dict)]
    deadlines = requirements.get("deadlines") or {}
    quota = requirements.get("quota") or {}
    how_to_submit = requirements.get("how_to_submit") or {}
    transcript_fields = {"bundle", "transcripts", "recording_ids"}
    return {
        "deliverables": [
            {
                "name": d.get("name"),
                "kind": d.get("kind"),
                "required": bool(d.get("required")),
                "description": d.get("description"),
            }
            for d in deliverables
        ],
        "checks": [
            {
                "check": c.get("check"),
                "title": c.get("title"),
                "phase": c.get("phase"),
                "applies": c.get("applies"),
                "description": c.get("description"),
                "fix": c.get("fix"),
            }
            for c in checks
        ],
        "deadlines": {
            "entries_close": deadlines.get("entries_close"),
            "final_window_start": deadlines.get("final_window_start"),
            "result_due": deadlines.get("result_due"),
            "challenge_ends": deadlines.get("challenge_ends"),
        },
        "quota": {
            "max_per_day": quota.get("max_per_day"),
            "max_entries_per_account": quota.get("max_entries_per_account"),
            "window": quota.get("window"),
        },
        "entry_fee": requirements.get("entry_fee"),
        "payout_curve": requirements.get("payout_curve"),
        "escrow": requirements.get("escrow"),
        "submission": {
            "method": how_to_submit.get("method"),
            "href": how_to_submit.get("href"),
            "content_type": how_to_submit.get("content_type"),
            "fields": how_to_submit.get("fields"),
            "cli": how_to_submit.get("cli"),
        },
        # Transcripts are not optional here: the checks match the submitted code
        # against the sessions that wrote it, so an entry without them is
        # refused. Reading it off the deliverables keeps that a fact from the
        # document rather than an assumption.
        "transcripts_required": any(
            str(d.get("name") or "") in transcript_fields and bool(d.get("required"))
            for d in deliverables
        ),
    }


async def ensure_project(
    db: AsyncSession,
    competition: Competition,
    *,
    owner_id: UUID | None = None,
    create_directory: bool = True,
) -> Project:
    """Bind a project workspace to a competition, creating it on first use."""
    if competition.project_id is not None:
        existing = await db.get(Project, competition.project_id)
        if existing is not None:
            return existing

    slug = f"bounty-{competition.external_id}"[:60].rstrip("-")
    result = await db.execute(select(Project).where(Project.slug == slug))
    project = result.scalar_one_or_none()
    if project is None:
        project = await workspace_service.create_project(
            db,
            slug=slug,
            name=f"Bounty: {competition.title}"[:200],
            description=(f"Workspace for the make.fun competition '{competition.external_id}'."),
            owner_id=owner_id,
            create_directory=create_directory,
        )
    competition.project_id = project.id
    await db.flush()
    return project


async def prepare_entry(
    db: AsyncSession,
    *,
    competition: Competition,
    repo_url: str,
    commit_sha: str,
    title: str,
    transcript_paths: list[str] | None = None,
    created_by: UUID | None = None,
) -> CompetitionEntry:
    """Record an entry payload locally and request approval to submit it.

    Nothing leaves the platform here. The entry stops at
    ``AWAITING_APPROVAL`` with an approval row carrying the exact payload, which
    is what makes the next step a human decision rather than a retry.
    """
    if not repo_url.strip():
        raise UnprocessableError("A submission needs the repository URL")
    if not commit_sha.strip():
        raise UnprocessableError("A submission needs the commit SHA")
    if competition.status == CompetitionStatus.CLOSED:
        raise ConflictError(f"Competition '{competition.external_id}' is closed")

    entry = CompetitionEntry(
        competition_id=competition.id,
        project_id=competition.project_id,
        created_by=created_by,
        status=CompetitionEntryStatus.AWAITING_APPROVAL,
        title=title.strip()[:300],
        repo_url=repo_url.strip(),
        commit_sha=commit_sha.strip(),
        transcript_paths=transcript_paths or [],
        meta={"prepared_at": datetime.now(UTC).isoformat()},
    )
    db.add(entry)
    await db.flush()

    approval = await approval_service.create_approval(
        db,
        title=f"Submit entry to {competition.external_id}",
        description=(
            f"Submit repo {entry.repo_url} at {entry.commit_sha[:12]} to the make.fun "
            f"competition '{competition.external_id}'. Submitting is public and cannot "
            f"be undone."
        ),
        action_type=ApprovalAction.SUBMIT_COMPETITION,
        assigned_to_user_id=created_by,
        payload={
            "competition": competition.external_id,
            "entry_id": str(entry.id),
            "repo_url": entry.repo_url,
            "commit_sha": entry.commit_sha,
            "title": entry.title,
            "transcripts": entry.transcript_paths,
        },
    )
    entry.approval_id = approval.id
    await db.flush()
    logger.info(
        "makefun_entry_prepared",
        slug=competition.external_id,
        entry=str(entry.id),
        approval=str(approval.id),
    )
    return entry


async def _approved_entry(db: AsyncSession, entry: CompetitionEntry) -> Approval:
    if entry.approval_id is None:
        raise ConflictError("This entry never requested approval")
    approval = await db.get(Approval, entry.approval_id)
    if approval is None:
        raise ConflictError("The approval record for this entry no longer exists")
    if approval.status != ApprovalStatus.APPROVED:
        raise ConflictError(
            f"Submission requires an approved approval; this one is {approval.status.value}"
        )
    return approval


async def submit_entry(
    db: AsyncSession,
    entry: CompetitionEntry,
    *,
    client: MakeFunClient | None = None,
    bus: RedisEventBus | None = None,
) -> CompetitionEntry:
    """Submit an approved entry to make.fun and record the verdict.

    The approval check comes first: a caller that never got sign-off cannot
    reach the network. A refusal is stored, not raised away — the check verdicts
    are the whole point of capturing a rejection.
    """
    await _approved_entry(db, entry)
    competition = await get_competition(db, entry.competition_id)

    own_client = client is None
    api = client or makefun_client()
    try:
        payload = await api.submit_entry(
            competition.external_id,
            repo_url=entry.repo_url,
            commit_sha=entry.commit_sha,
            title=entry.title or None,
        )
    except MakeFunProblem as exc:
        entry.status = CompetitionEntryStatus.REJECTED
        entry.checks = exc.checks
        entry.error = exc.message[:4_000]
        entry.payload = {**(entry.payload or {}), "problem": exc.problem}
        entry.last_synced_at = datetime.now(UTC)
        competition.status = CompetitionStatus.EVALUATING
        await db.flush()
        logger.warning(
            "makefun_entry_rejected", entry=str(entry.id), stage=exc.stage, checks=len(exc.checks)
        )
        raise
    finally:
        if own_client:
            await api.aclose()

    submission_id = str(
        payload.get("submission_id")
        or payload.get("id")
        or (payload.get("submission") or {}).get("id")
        or ""
    ).strip()
    entry.submission_id = submission_id or None
    entry.status = CompetitionEntryStatus.SUBMITTED
    entry.submitted_at = datetime.now(UTC)
    entry.last_synced_at = entry.submitted_at
    entry.payload = {**(entry.payload or {}), "response": payload}
    competition.status = CompetitionStatus.SUBMITTED
    competition.submitted_at = entry.submitted_at
    await db.flush()

    if bus is not None:
        with contextlib.suppress(Exception):
            await bus.publish(
                Event(
                    type=EventType.SUBMISSION_COMPLETED,
                    payload={
                        "competition": competition.external_id,
                        "entry_id": str(entry.id),
                        "submission_id": entry.submission_id,
                    },
                )
            )
    logger.info(
        "makefun_entry_submitted",
        slug=competition.external_id,
        entry=str(entry.id),
        submission=entry.submission_id,
    )
    return entry


_STATUS_MAP: dict[str, CompetitionEntryStatus] = {
    "queued": CompetitionEntryStatus.SUBMITTED,
    "pending": CompetitionEntryStatus.VALIDATING,
    "validating": CompetitionEntryStatus.VALIDATING,
    "running": CompetitionEntryStatus.VALIDATING,
    "passed": CompetitionEntryStatus.GRADED,
    "graded": CompetitionEntryStatus.GRADED,
    "placed": CompetitionEntryStatus.PLACED,
    "won": CompetitionEntryStatus.WON,
    "failed": CompetitionEntryStatus.FAILED,
    "rejected": CompetitionEntryStatus.REJECTED,
    "finished": CompetitionEntryStatus.GRADED,
    "withdrawn": CompetitionEntryStatus.WITHDRAWN,
}


async def refresh_entry_status(
    db: AsyncSession,
    entry: CompetitionEntry,
    *,
    client: MakeFunClient | None = None,
) -> CompetitionEntry:
    """Pull the platform's verdict for a submitted entry."""
    if not entry.submission_id:
        raise ConflictError("This entry has no submission id yet")

    own_client = client is None
    api = client or makefun_client()
    try:
        payload = await api.get_submission_status(entry.submission_id)
    finally:
        if own_client:
            await api.aclose()

    raw_status = str(payload.get("status") or "").lower()
    if raw_status in _STATUS_MAP:
        entry.status = _STATUS_MAP[raw_status]

    checks = payload.get("checks")
    if isinstance(checks, list):
        entry.checks = [c for c in checks if isinstance(c, dict)]
    if payload.get("score") is not None:
        entry.score = str(payload["score"])[:64]
    if payload.get("rank") is not None:
        with contextlib.suppress(TypeError, ValueError):
            entry.rank = int(payload["rank"])
    payout = payload.get("payout")
    if isinstance(payout, dict) and payout.get("amountUsd") is not None:
        entry.prize_usd = _money(payout.get("amountUsd"))
    if payload.get("error"):
        entry.error = str(payload["error"])[:4_000]

    entry.last_synced_at = datetime.now(UTC)
    entry.payload = {**(entry.payload or {}), "status_response": payload}
    await db.flush()
    return entry


async def list_entries(
    db: AsyncSession,
    *,
    competition_id: UUID | None = None,
    status: CompetitionEntryStatus | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[CompetitionEntry]:
    statement = (
        select(CompetitionEntry)
        .order_by(CompetitionEntry.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if competition_id is not None:
        statement = statement.where(CompetitionEntry.competition_id == competition_id)
    if status is not None:
        statement = statement.where(CompetitionEntry.status == status)
    result = await db.execute(statement)
    return list(result.scalars())


async def get_entry(db: AsyncSession, entry_id: UUID) -> CompetitionEntry:
    entry = await db.get(CompetitionEntry, entry_id)
    if entry is None:
        raise NotFoundError("Entry not found")
    return entry


async def check_quota(
    competition: Competition, *, client: MakeFunClient | None = None
) -> dict[str, Any]:
    """Ask the platform how many entries are left before building anything."""
    own_client = client is None
    api = client or makefun_client()
    try:
        return await api.get_quota(competition.external_id)
    finally:
        if own_client:
            await api.aclose()
