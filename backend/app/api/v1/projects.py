"""Project endpoints: workspaces, files, commands, repositories, coding engines.

Every filesystem route resolves through :class:`app.workspace.paths.Workspace`,
so the HTTP surface and the agent tools enforce exactly the same confinement.
Write and execute routes are operator-only and audited.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Query, Request, status
from sqlalchemy import select

from app.api.deps import AdminPrincipal, DbSession, OperatorPrincipal, ViewerPrincipal
from app.coding import CodingRequest, default_engine_registry
from app.core.config import get_settings
from app.core.exceptions import NotFoundError
from app.db.models import CodingSession, Project, Repository
from app.schemas.project import (
    CodingChange,
    CodingEngineInfo,
    CodingRunRequest,
    CodingRunResponse,
    CodingSessionPublic,
    CommandRequest,
    CommandResponse,
    FileContent,
    FileWriteRequest,
    FileWriteResponse,
    ProjectCreate,
    ProjectPublic,
    RepositoryCreate,
    RepositoryPublic,
    SearchMatch,
    SearchRequest,
    SearchResponse,
    WorkspaceFile,
    WorkspaceListing,
    WorkspaceTree,
)
from app.services import workspaces as workspace_service
from app.services.audit import record_audit
from app.workspace.sandbox import resolve_runner

router = APIRouter(prefix="/projects", tags=["projects"])

_engines = default_engine_registry()


@router.get("", response_model=list[ProjectPublic], summary="List projects")
async def list_projects(
    principal: ViewerPrincipal,
    db: DbSession,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[ProjectPublic]:
    result = await db.execute(
        select(Project).order_by(Project.created_at.desc()).limit(limit).offset(offset)
    )
    return [ProjectPublic.model_validate(project) for project in result.scalars()]


@router.post(
    "",
    response_model=ProjectPublic,
    status_code=status.HTTP_201_CREATED,
    summary="Create a project and its workspace",
)
async def create_project(
    payload: ProjectCreate,
    principal: OperatorPrincipal,
    db: DbSession,
    request: Request,
) -> ProjectPublic:
    project = await workspace_service.create_project(
        db,
        slug=payload.slug,
        name=payload.name,
        description=payload.description,
        owner_id=principal.user.id,
    )
    await record_audit(
        db,
        action="project.created",
        resource_type="project",
        resource_id=project.id,
        actor_user_id=principal.user.id,
        meta={"slug": project.slug},
        request=request,
    )
    await db.commit()
    return ProjectPublic.model_validate(project)


@router.get("/engines", response_model=list[CodingEngineInfo], summary="List coding engines")
async def list_engines(principal: ViewerPrincipal) -> list[CodingEngineInfo]:
    described = await _engines.describe_all()
    return [CodingEngineInfo(**spec) for spec in described]


@router.get("/{project_id}", response_model=ProjectPublic, summary="Get a project")
async def get_project(project_id: UUID, principal: ViewerPrincipal, db: DbSession) -> ProjectPublic:
    project = await workspace_service.get_project(db, project_id)
    return ProjectPublic.model_validate(project)


@router.get("/{project_id}/workspace", response_model=WorkspaceListing, summary="Workspace status")
async def get_workspace(
    project_id: UUID, principal: ViewerPrincipal, db: DbSession
) -> WorkspaceListing:
    project = await workspace_service.get_project(db, project_id)
    info = await workspace_service.describe_project(db, project)
    return WorkspaceListing(
        project_id=project_id,
        slug=info.slug,
        path=str(info.path),
        exists=info.exists,
        file_count=info.file_count,
        total_bytes=info.total_bytes,
        repositories=info.repositories,
    )


@router.get("/{project_id}/files", response_model=WorkspaceTree, summary="List workspace files")
async def list_files(
    project_id: UUID,
    principal: ViewerPrincipal,
    db: DbSession,
    path: str = Query(default="."),
    limit: int = Query(default=500, ge=1, le=2000),
) -> WorkspaceTree:
    project = await workspace_service.get_project(db, project_id)
    workspace = await workspace_service.ensure_workspace(project)
    entries = await workspace.list_tree(path, max_entries=limit)
    return WorkspaceTree(
        project_id=project_id,
        path=path,
        entries=[WorkspaceFile(**entry) for entry in entries],
        count=len(entries),
    )


@router.get("/{project_id}/file", response_model=FileContent, summary="Read a workspace file")
async def read_file(
    project_id: UUID,
    principal: ViewerPrincipal,
    db: DbSession,
    path: str = Query(min_length=1),
    max_bytes: int = Query(default=200_000, ge=100, le=1_000_000),
) -> FileContent:
    project = await workspace_service.get_project(db, project_id)
    workspace = await workspace_service.ensure_workspace(project)
    payload = await workspace.read_bytes(path, max_bytes=max_bytes)
    text = payload.decode("utf-8", errors="replace")
    return FileContent(
        project_id=project_id,
        path=path,
        bytes=len(payload),
        lines=len(text.splitlines()),
        content=text,
    )


@router.put(
    "/{project_id}/file", response_model=FileWriteResponse, summary="Write a workspace file"
)
async def write_file(
    project_id: UUID,
    payload: FileWriteRequest,
    principal: OperatorPrincipal,
    db: DbSession,
    request: Request,
) -> FileWriteResponse:
    project = await workspace_service.get_project(db, project_id)
    workspace = await workspace_service.ensure_workspace(project)
    target = await workspace.resolve(payload.path)
    existed = await asyncio.to_thread(Path.exists, target)
    path = await workspace.write_bytes(payload.path, payload.content.encode("utf-8"))
    await record_audit(
        db,
        action="project.file_written",
        resource_type="project",
        resource_id=project.id,
        actor_user_id=principal.user.id,
        meta={"path": workspace.relative(path), "bytes": len(payload.content)},
        request=request,
    )
    await db.commit()
    return FileWriteResponse(
        project_id=project_id,
        path=workspace.relative(path),
        bytes=len(payload.content.encode("utf-8")),
        created=not existed,
    )


@router.delete(
    "/{project_id}/file",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a workspace path",
)
async def delete_file(
    project_id: UUID,
    principal: OperatorPrincipal,
    db: DbSession,
    request: Request,
    path: str = Query(min_length=1),
) -> None:
    project = await workspace_service.get_project(db, project_id)
    workspace = await workspace_service.ensure_workspace(project)
    deleted = await workspace.delete(path)
    await record_audit(
        db,
        action="project.file_deleted",
        resource_type="project",
        resource_id=project.id,
        actor_user_id=principal.user.id,
        meta={"path": workspace.relative(deleted)},
        request=request,
    )
    await db.commit()


@router.post("/{project_id}/search", response_model=SearchResponse, summary="Search workspace code")
async def search_code(
    project_id: UUID,
    payload: SearchRequest,
    principal: OperatorPrincipal,
    db: DbSession,
) -> SearchResponse:
    project = await workspace_service.get_project(db, project_id)
    workspace = await workspace_service.ensure_workspace(project)
    matches = await workspace.search_text(
        payload.pattern, glob=payload.glob, max_results=payload.max_results
    )
    return SearchResponse(
        project_id=project_id,
        pattern=payload.pattern,
        matches=[SearchMatch(**match) for match in matches],
        count=len(matches),
    )


@router.post(
    "/{project_id}/commands", response_model=CommandResponse, summary="Run a workspace command"
)
async def run_command(
    project_id: UUID,
    payload: CommandRequest,
    principal: AdminPrincipal,
    db: DbSession,
    request: Request,
) -> CommandResponse:
    settings = get_settings()
    project = await workspace_service.get_project(db, project_id)
    workspace = await workspace_service.ensure_workspace(project)
    report = await resolve_runner(
        sandbox_enabled=settings.sandbox_enabled,
        sandbox_image=settings.sandbox_image,
        allow_host_execution=settings.sandbox_allow_host_execution,
    )
    result = await report.runner.run(
        payload.command,
        workspace=workspace,
        timeout_seconds=payload.timeout_seconds,
    )
    await record_audit(
        db,
        action="project.command_run",
        resource_type="project",
        resource_id=project.id,
        actor_user_id=principal.user.id,
        meta={
            "command": payload.command[:200],
            "exit_code": result.exit_code,
            "sandbox": result.sandbox,
        },
        request=request,
    )
    await db.commit()
    return CommandResponse(project_id=project_id, **result.as_dict())


@router.post(
    "/{project_id}/repositories",
    response_model=RepositoryPublic,
    status_code=status.HTTP_201_CREATED,
    summary="Clone and register a repository",
)
async def add_repository(
    project_id: UUID,
    payload: RepositoryCreate,
    principal: AdminPrincipal,
    db: DbSession,
    request: Request,
) -> RepositoryPublic:
    project = await workspace_service.get_project(db, project_id)
    await workspace_service.ensure_workspace(project)
    name = await workspace_service.clone_repository(
        project, url=payload.url, branch=payload.branch, name=payload.name
    )
    repository = Repository(
        project_id=project.id,
        name=name,
        url=payload.url,
        default_branch=payload.branch or "main",
        local_path=str(Path(project.workspace_path) / name),
    )
    db.add(repository)
    await record_audit(
        db,
        action="project.repository_cloned",
        resource_type="project",
        resource_id=project.id,
        actor_user_id=principal.user.id,
        meta={"url": payload.url, "name": name},
        request=request,
    )
    await db.commit()
    await db.refresh(repository)
    return RepositoryPublic.model_validate(repository)


@router.post(
    "/{project_id}/coding/run", response_model=CodingRunResponse, summary="Run the coding engine"
)
async def run_coding_engine(
    project_id: UUID,
    payload: CodingRunRequest,
    principal: AdminPrincipal,
    db: DbSession,
    request: Request,
) -> CodingRunResponse:
    settings = get_settings()
    project = await workspace_service.get_project(db, project_id)
    workspace = await workspace_service.ensure_workspace(project)
    report = await resolve_runner(
        sandbox_enabled=settings.sandbox_enabled,
        sandbox_image=settings.sandbox_image,
        allow_host_execution=settings.sandbox_allow_host_execution,
    )
    engine = await _engines.resolve(payload.engine)
    result = await engine.run(
        CodingRequest(
            instruction=payload.instruction,
            workspace=workspace,
            model=payload.model,
            session_id=payload.session_id,
            timeout_seconds=payload.timeout_seconds,
        ),
        report.runner,
    )
    await record_audit(
        db,
        action="project.coding_run",
        resource_type="project",
        resource_id=project.id,
        actor_user_id=principal.user.id,
        meta={
            "engine": result.engine,
            "ok": result.ok,
            "session_id": result.session_id,
            "files": len(result.changes),
        },
        request=request,
    )
    await workspace_service.record_coding_session(
        db,
        project=project,
        result=result,
        instruction=payload.instruction,
        model=payload.model,
        requested_by=principal.user.id,
    )
    await db.commit()
    return CodingRunResponse(
        project_id=project_id,
        engine=result.engine,
        ok=result.ok,
        session_id=result.session_id,
        summary=result.summary,
        changes=[CodingChange(**change) for change in result.changes],
        commands=result.commands,
        transcript_path=result.transcript_path,
        duration_ms=result.duration_ms,
        exit_code=result.exit_code,
        error=result.error,
    )


@router.get(
    "/{project_id}/coding/sessions",
    response_model=list[CodingSessionPublic],
    summary="List coding-engine sessions",
)
async def list_coding_sessions(
    project_id: UUID,
    principal: ViewerPrincipal,
    db: DbSession,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[CodingSessionPublic]:
    await workspace_service.get_project(db, project_id)
    result = await db.execute(
        select(CodingSession)
        .where(CodingSession.project_id == project_id)
        .order_by(CodingSession.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return [CodingSessionPublic.model_validate(row) for row in result.scalars()]


@router.get(
    "/{project_id}/coding/sessions/{session_pk}",
    response_model=CodingSessionPublic,
    summary="Get a coding-engine session",
)
async def get_coding_session(
    project_id: UUID, session_pk: UUID, principal: ViewerPrincipal, db: DbSession
) -> CodingSessionPublic:
    session = await db.get(CodingSession, session_pk)
    if session is None or session.project_id != project_id:
        raise NotFoundError("Coding session not found")
    return CodingSessionPublic.model_validate(session)
