"""make.fun tool resources: the callables the bounty tools invoke.

The tools themselves stay persistence-free; this module is where a tool call
becomes a short transaction. Two behaviours are deliberate:

* The listing reads the local mirror. A model that asks for competitions gets
  what a sync actually stored, so it cannot present an unverified listing as
  fact. ``refresh=True`` performs a real sync first and says so in the result.
* ``makefun_prepare_entry`` records the payload and opens an approval. The
  submission callable refuses unless *both* the run-level critical-tool approval
  and the entry's own approval say yes — an agent cannot approve itself by
  preparing an entry.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.exceptions import ConflictError, NotFoundError
from app.db.enums import ApprovalStatus, CompetitionStatus
from app.db.models import Approval, Competition
from app.services import bounty as bounty_service

logger = structlog.get_logger("agentos.services.bounty.resources")


def _public(competition: Competition) -> dict[str, Any]:
    return {
        "id": str(competition.id),
        "slug": competition.external_id,
        "title": competition.title,
        "status": competition.status.value,
        "reward_usd": str(competition.reward_amount) if competition.reward_amount else None,
        "deadline": competition.deadline_at.isoformat() if competition.deadline_at else None,
        "url": competition.url,
        "has_requirements": bool(competition.requirements),
        "meta": {
            key: (competition.meta or {}).get(key)
            for key in ("sponsor", "category", "skills", "submission_count", "listing_status")
        },
    }


def makefun_resources(
    factory: async_sessionmaker[AsyncSession],
    *,
    project_id: UUID | None,
    user_id: UUID | None,
) -> dict[str, Any]:
    """Build the callables the make.fun tools use."""

    async def list_competitions(
        *, refresh: bool = False, status: str | None = None, limit: int = 20
    ) -> dict[str, Any]:
        async with factory() as session:
            synced = None
            if refresh:
                synced = await bounty_service.sync_competitions(session, limit=limit, status=status)
                await session.commit()
            items, total = await bounty_service.list_competitions(session, limit=limit)
            return {
                "source": "make.fun sync" if refresh else "local mirror",
                "synced": synced,
                "total_known": total,
                "items": [_public(item) for item in items],
            }

    async def competition_details(slug: str, *, refresh: bool = False) -> dict[str, Any]:
        async with factory() as session:
            competition = await bounty_service.get_competition_by_slug(session, slug)
            if refresh or not competition.requirements:
                requirements = await bounty_service.refresh_requirements(session, competition)
            else:
                requirements = competition.requirements
            await session.commit()
            return {
                "competition": _public(competition),
                "requirements": bounty_service.requirements_summary(requirements or {}),
            }

    async def prepare_entry(
        *,
        slug: str,
        repo_url: str,
        commit_sha: str,
        title: str = "",
        transcript_paths: list[str] | None = None,
    ) -> dict[str, Any]:
        async with factory() as session:
            competition = await bounty_service.get_competition_by_slug(session, slug)
            if competition.status == CompetitionStatus.CLOSED:
                raise ConflictError(f"Competition '{slug}' is closed to entries")
            project = None
            if project_id is not None:
                project = await bounty_service.ensure_project(
                    session, competition, owner_id=user_id
                )
            entry = await bounty_service.prepare_entry(
                session,
                competition=competition,
                repo_url=repo_url,
                commit_sha=commit_sha,
                title=title,
                transcript_paths=transcript_paths or [],
                created_by=user_id,
            )
            await session.commit()
            return {
                "entry_id": str(entry.id),
                "status": entry.status.value,
                "approval_id": str(entry.approval_id),
                "project_id": str(project.id) if project is not None else None,
                "next_step": (
                    "A human must approve this entry before it can be submitted. "
                    "Nothing has been sent to make.fun."
                ),
            }

    async def submit_entry(entry_id: str) -> dict[str, Any]:
        async with factory() as session:
            try:
                parsed = UUID(entry_id)
            except ValueError as exc:
                raise ConflictError("The entry id is not a UUID") from exc
            try:
                entry = await bounty_service.get_entry(session, parsed)
            except NotFoundError as exc:
                raise ConflictError("That entry does not exist") from exc

            if entry.approval_id is None:
                raise ConflictError("This entry has no approval record")
            approval = await session.get(Approval, entry.approval_id)
            if approval is None or approval.status != ApprovalStatus.APPROVED:
                raise ConflictError(
                    "The entry's approval is not granted; a human must approve it first"
                )

            submitted = await bounty_service.submit_entry(session, entry)
            await session.commit()
            return {
                "entry_id": str(submitted.id),
                "status": submitted.status.value,
                "submission_id": submitted.submission_id,
                "submitted_at": (
                    submitted.submitted_at.isoformat() if submitted.submitted_at else None
                ),
            }

    return {
        "makefun_competitions": list_competitions,
        "makefun_competition_details": competition_details,
        "makefun_prepare_entry": prepare_entry,
        "makefun_submit_entry": submit_entry,
    }
