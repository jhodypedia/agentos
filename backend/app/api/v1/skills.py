"""Skill endpoints: catalogue, install, import, validate, assign, history."""

from __future__ import annotations

import tempfile
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Query, Request, status

from app.api.deps import AdminPrincipal, DbSession, OperatorPrincipal
from app.core.config import get_settings
from app.core.exceptions import NotFoundError
from app.db.models import Skill
from app.schemas.skill import (
    SkillCreate,
    SkillDetail,
    SkillExecutionPublic,
    SkillImportRequest,
    SkillPublic,
    SkillSyncResponse,
    SkillUpdate,
    SkillValidationRequest,
    SkillValidationResponse,
)
from app.services import agents as agent_service
from app.services import skills as skill_service
from app.services.audit import record_audit
from app.skills.loader import SkillError, parse_skill_file

router = APIRouter(prefix="/skills", tags=["skills"])


def _detail(
    skill: Skill,
    *,
    body: str = "",
    files: list[str] | None = None,
    assigned: list[UUID] | None = None,
) -> SkillDetail:
    detail = SkillDetail.model_validate(skill)
    detail.body = body
    detail.files = files or []
    detail.assigned_agent_ids = assigned or []
    return detail


@router.get("", response_model=list[SkillPublic], summary="List installed skills")
async def list_skills(
    principal: OperatorPrincipal,
    db: DbSession,
    query: str | None = Query(default=None, description="Case-insensitive substring search"),
    enabled: bool | None = Query(default=None),
    source: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[SkillPublic]:
    skills, _total = await skill_service.list_skills(
        db, query=query, enabled=enabled, source=source, limit=limit, offset=offset
    )
    return [SkillPublic.model_validate(skill) for skill in skills]


@router.get("/search", response_model=list[SkillPublic], summary="Search installed skills")
async def search_skills(
    principal: OperatorPrincipal,
    db: DbSession,
    q: str = Query(min_length=1, max_length=200),
    limit: int = Query(default=20, ge=1, le=100),
) -> list[SkillPublic]:
    skills, _total = await skill_service.list_skills(db, query=q, limit=limit)
    return [SkillPublic.model_validate(skill) for skill in skills]


@router.post(
    "",
    response_model=SkillDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Create a custom skill",
)
async def create_skill(
    payload: SkillCreate,
    principal: AdminPrincipal,
    db: DbSession,
    request: Request,
) -> SkillDetail:
    skill = await skill_service.create_custom_skill(
        db,
        slug=payload.slug,
        name=payload.name,
        description=payload.description,
        version=payload.version,
        body=payload.body,
        frontmatter=payload.frontmatter,
        enabled=payload.enabled,
        created_by=principal.user.id,
    )
    body = await skill_service.load_skill_body(db, skill)

    await record_audit(
        db,
        action="skill.created",
        resource_type="skill",
        resource_id=skill.id,
        actor_user_id=principal.user.id,
        request=request,
        meta={"slug": skill.slug, "version": skill.version},
    )
    await db.commit()
    return _detail(
        skill,
        body=body,
        files=await skill_service.list_skill_files(skill),
    )


@router.post(
    "/sync",
    response_model=SkillSyncResponse,
    summary="Rescan the skills directory",
)
async def sync_skills(
    principal: AdminPrincipal, db: DbSession, request: Request
) -> SkillSyncResponse:
    report = await skill_service.sync_from_directory(db, source="local", overwrite=True)
    await record_audit(
        db,
        action="skill.synced",
        resource_type="skill",
        actor_user_id=principal.user.id,
        request=request,
        meta=report.as_dict(),
    )
    await db.commit()
    return SkillSyncResponse(**report.as_dict())


@router.post(
    "/import",
    response_model=list[SkillPublic],
    status_code=status.HTTP_201_CREATED,
    summary="Import skills from a git repository",
)
async def import_skills(
    payload: SkillImportRequest,
    principal: AdminPrincipal,
    db: DbSession,
    request: Request,
) -> list[SkillPublic]:
    skills = await skill_service.install_from_git(
        db,
        payload.repo_url,
        ref=payload.ref,
        subdirectory=payload.subdirectory,
        created_by=principal.user.id,
        overwrite=payload.overwrite,
    )
    await record_audit(
        db,
        action="skill.imported",
        resource_type="skill",
        actor_user_id=principal.user.id,
        request=request,
        meta={
            "repo_url": payload.repo_url,
            "ref": payload.ref,
            "slugs": [skill.slug for skill in skills],
        },
    )
    await db.commit()
    return [SkillPublic.model_validate(skill) for skill in skills]


@router.post(
    "/validate",
    response_model=SkillValidationResponse,
    summary="Validate skill metadata without installing it",
)
async def validate_skill(
    payload: SkillValidationRequest,
    principal: OperatorPrincipal,
    db: DbSession,
) -> SkillValidationResponse:
    """Validate a path under the skills root, or raw SKILL.md content."""
    if not payload.has_input:
        return SkillValidationResponse(
            valid=False, errors=["Provide 'path' (under the skills root) or 'content'"]
        )
    settings = get_settings()
    raw_path = payload.path
    if raw_path:
        candidate = (settings.skills_root / raw_path).resolve()
        try:
            candidate.relative_to(settings.skills_root.resolve())
        except ValueError:
            return SkillValidationResponse(valid=False, errors=["Path escapes the skills root"])
        entry = candidate / "SKILL.md" if candidate.is_dir() else candidate
        try:
            document = parse_skill_file(entry, default_slug=entry.parent.name)
        except SkillError as exc:
            return SkillValidationResponse(valid=False, errors=[exc.message])
        return SkillValidationResponse(valid=True, slug=document.slug, version=document.version)

    content = payload.content or ""
    with tempfile.TemporaryDirectory() as workspace:
        entry = Path(workspace) / "SKILL.md"
        entry.write_text(content, encoding="utf-8")
        try:
            document = parse_skill_file(entry, default_slug=payload.slug)
        except SkillError as exc:
            return SkillValidationResponse(valid=False, errors=[exc.message])
    warnings: list[str] = []
    if not document.allowed_tools:
        warnings.append("No allowed-tools declared; the skill cannot use any tools")
    return SkillValidationResponse(
        valid=True, slug=document.slug, version=document.version, warnings=warnings
    )


@router.get("/{skill_id}", response_model=SkillDetail, summary="Get a skill")
async def get_skill(skill_id: UUID, principal: OperatorPrincipal, db: DbSession) -> SkillDetail:
    skill = await skill_service.get_skill(db, skill_id)
    try:
        body = await skill_service.load_skill_body(db, skill)
    except NotFoundError:
        body = ""
    return _detail(
        skill,
        body=body,
        files=await skill_service.list_skill_files(skill),
        assigned=await skill_service.assigned_agent_ids(db, skill.id),
    )


@router.patch("/{skill_id}", response_model=SkillPublic, summary="Enable or disable a skill")
async def update_skill(
    skill_id: UUID,
    payload: SkillUpdate,
    principal: AdminPrincipal,
    db: DbSession,
    request: Request,
) -> SkillPublic:
    skill = await skill_service.get_skill(db, skill_id)
    if payload.enabled is not None:
        await skill_service.set_skill_enabled(db, skill, payload.enabled)
    if payload.description is not None:
        skill.description = payload.description
        await db.flush()
    await record_audit(
        db,
        action="skill.updated",
        resource_type="skill",
        resource_id=skill.id,
        actor_user_id=principal.user.id,
        request=request,
        meta={"enabled": skill.enabled},
    )
    await db.commit()
    await db.refresh(skill)
    return SkillPublic.model_validate(skill)


@router.delete("/{skill_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a skill")
async def delete_skill(
    skill_id: UUID, principal: AdminPrincipal, db: DbSession, request: Request
) -> None:
    skill = await skill_service.get_skill(db, skill_id)
    slug = skill.slug
    await skill_service.delete_skill(db, skill)
    await record_audit(
        db,
        action="skill.deleted",
        resource_type="skill",
        resource_id=skill_id,
        actor_user_id=principal.user.id,
        request=request,
        meta={"slug": slug},
    )
    await db.commit()


@router.post(
    "/{skill_id}/assign/{agent_id}",
    response_model=SkillDetail,
    summary="Assign a skill to an agent",
)
async def assign_to_agent(
    skill_id: UUID,
    agent_id: UUID,
    principal: AdminPrincipal,
    db: DbSession,
    request: Request,
    enabled: bool = Query(default=True),
) -> SkillDetail:
    skill = await skill_service.get_skill(db, skill_id)
    agent = await agent_service.get_agent(db, agent_id)
    await skill_service.assign_skill(db, agent, skill, enabled=enabled)
    await record_audit(
        db,
        action="skill.assigned",
        resource_type="skill",
        resource_id=skill.id,
        actor_user_id=principal.user.id,
        request=request,
        meta={"agent_slug": agent.slug, "enabled": enabled},
    )
    await db.commit()
    return _detail(skill, assigned=await skill_service.assigned_agent_ids(db, skill.id))


@router.delete(
    "/{skill_id}/assign/{agent_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a skill from an agent",
)
async def unassign_from_agent(
    skill_id: UUID,
    agent_id: UUID,
    principal: AdminPrincipal,
    db: DbSession,
    request: Request,
) -> None:
    skill = await skill_service.get_skill(db, skill_id)
    agent = await agent_service.get_agent(db, agent_id)
    await skill_service.unassign_skill(db, agent, skill)
    await record_audit(
        db,
        action="skill.unassigned",
        resource_type="skill",
        resource_id=skill.id,
        actor_user_id=principal.user.id,
        request=request,
        meta={"agent_slug": agent.slug},
    )
    await db.commit()


@router.get(
    "/{skill_id}/executions",
    response_model=list[SkillExecutionPublic],
    summary="Skill execution history",
)
async def skill_executions(
    skill_id: UUID,
    principal: OperatorPrincipal,
    db: DbSession,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[SkillExecutionPublic]:
    await skill_service.get_skill(db, skill_id)
    rows = await skill_service.execution_history(db, skill_id=skill_id, limit=limit)
    return [SkillExecutionPublic.model_validate(row) for row in rows]


@router.get(
    "/{skill_id}/versions",
    response_model=list[SkillPublic],
    summary="Every installed version of a skill",
)
async def skill_versions(
    skill_id: UUID, principal: OperatorPrincipal, db: DbSession
) -> list[SkillPublic]:
    skill = await skill_service.get_skill(db, skill_id)
    versions, _total = await skill_service.list_skills(db, query=None, limit=200)
    return [SkillPublic.model_validate(item) for item in versions if item.slug == skill.slug]
