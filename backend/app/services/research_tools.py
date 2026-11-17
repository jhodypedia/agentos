from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.exceptions import PermissionDeniedError
from app.db.models import ResearchScope
from app.schemas.research import FindingCreate, FindingPublic, ScopePublic
from app.services import research


def research_resources(
    factory: async_sessionmaker[AsyncSession], *, project_id: UUID | None, user_id: UUID | None
) -> dict[str, Any]:
    def require_project() -> None:
        if project_id is None:
            raise PermissionDeniedError("Research tools require a task project")

    async def scopes() -> dict[str, Any]:
        require_project()
        async with factory() as db:
            items = await db.scalars(
                select(ResearchScope).where(ResearchScope.project_id == project_id).limit(100)
            )
            return {
                "scopes": [ScopePublic.model_validate(s).model_dump(mode="json") for s in items]
            }

    async def hypothesis(scope_id: str, title: str, hypothesis: str) -> dict[str, Any]:
        require_project()
        async with factory() as db:
            scope = await research.get_scope(db, UUID(scope_id))
            if scope.project_id != project_id:
                raise PermissionDeniedError("Scope belongs to another project")
            finding = await research.create_finding(
                db, FindingCreate(scope_id=scope.id, title=title, hypothesis=hypothesis), user_id
            )
            await db.commit()
            return FindingPublic.model_validate(finding).model_dump(mode="json")

    async def report(finding_id: str) -> dict[str, Any]:
        require_project()
        async with factory() as db:
            finding = await research.get_finding(db, UUID(finding_id))
            if finding.project_id != project_id:
                raise PermissionDeniedError("Finding belongs to another project")
            return await research.report(db, finding)

    return {"research_scopes": scopes, "research_hypothesis": hypothesis, "research_report": report}
