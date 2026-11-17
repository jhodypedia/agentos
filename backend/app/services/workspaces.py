"""Project workspaces: create, adopt and inspect the directory an agent edits.

A workspace is a plain directory under ``WORKSPACE_ROOT``. Its name is derived
from the project slug and validated, so a project called ``../etc`` cannot
produce a workspace outside the root. The database row stores the absolute path;
this service is the only place that decides what that path may be.
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.coding import CodingResult
from app.core.config import get_settings
from app.core.exceptions import ConflictError, NotFoundError, UnprocessableError
from app.db.enums import CodingSessionStatus, ProjectStatus
from app.db.models import CodingSession, Project, Repository
from app.workspace.paths import Workspace, validate_workspace_slug

logger = structlog.get_logger("agentos.workspaces")


@dataclass(slots=True)
class WorkspaceInfo:
    project_id: UUID
    slug: str
    path: Path
    exists: bool
    file_count: int
    total_bytes: int
    repositories: int


def workspace_root() -> Path:
    return Path(get_settings().workspace_root)


def workspace_path_for(slug: str) -> Path:
    """Absolute path for a project slug, validated and confined to the root."""
    safe = validate_workspace_slug(slug)
    root = workspace_root().resolve()
    return root / safe


async def get_project(db: AsyncSession, project_id: UUID) -> Project:
    project = await db.get(Project, project_id)
    if project is None:
        raise NotFoundError("Project not found")
    return project


async def get_project_by_slug(db: AsyncSession, slug: str) -> Project:
    result = await db.execute(select(Project).where(Project.slug == slug))
    project = result.scalar_one_or_none()
    if project is None:
        raise NotFoundError(f"Project '{slug}' not found")
    return project


async def create_project(
    db: AsyncSession,
    *,
    slug: str,
    name: str,
    description: str = "",
    owner_id: UUID | None = None,
    create_directory: bool = True,
) -> Project:
    """Create a project row and its workspace directory."""
    safe = validate_workspace_slug(slug)
    existing = await db.execute(select(Project).where(Project.slug == safe))
    if existing.scalar_one_or_none() is not None:
        raise ConflictError(f"A project with slug '{safe}' already exists")

    path = workspace_path_for(safe)
    if create_directory:
        await asyncio.to_thread(path.mkdir, parents=True, exist_ok=True)

    project = Project(
        slug=safe,
        name=name.strip()[:200] or safe,
        description=description,
        owner_id=owner_id,
        workspace_path=str(path),
        status=ProjectStatus.ACTIVE,
    )
    db.add(project)
    await db.flush()
    logger.info("project_created", slug=safe, workspace=str(path))
    return project


async def open_workspace(project: Project) -> Workspace:
    if project.status != ProjectStatus.ACTIVE:
        raise UnprocessableError(f"Project '{project.slug}' is {project.status.value}")
    path = Path(project.workspace_path)
    await _assert_inside_project(path, project)
    return await Workspace.open(path)


async def ensure_workspace(project: Project) -> Workspace:
    """Open the workspace, creating the directory if it was removed."""
    path = Path(project.workspace_path)
    await _assert_inside_project(path, project)
    return await Workspace.create(path)


async def _assert_inside_project(path: Path, project: Project) -> None:
    """Refuse a database path that does not match the project's own workspace."""
    expected = workspace_path_for(project.slug)
    resolved = await asyncio.to_thread(path.resolve)
    expected_resolved = await asyncio.to_thread(expected.resolve)
    if resolved != expected_resolved:
        raise UnprocessableError(f"Project '{project.slug}' points outside its workspace root")


async def describe_project(db: AsyncSession, project: Project) -> WorkspaceInfo:
    path = Path(project.workspace_path)
    exists = await asyncio.to_thread(path.exists)
    file_count = 0
    total_bytes = 0
    if exists:
        file_count, total_bytes = await asyncio.to_thread(_measure, path)

    result = await db.execute(select(Repository.id).where(Repository.project_id == project.id))
    repositories = len(list(result.scalars()))
    return WorkspaceInfo(
        project_id=project.id,
        slug=project.slug,
        path=path,
        exists=exists,
        file_count=file_count,
        total_bytes=total_bytes,
        repositories=repositories,
    )


def _measure(path: Path) -> tuple[int, int]:
    files = 0
    size = 0
    for entry in path.rglob("*"):
        if entry.is_file():
            files += 1
            try:
                size += entry.stat().st_size
            except OSError:
                continue
    return files, size


async def clone_repository(
    project: Project,
    *,
    url: str,
    branch: str | None = None,
    name: str | None = None,
    timeout_seconds: int = 300,
) -> str:
    """Clone an authorised repository into the project workspace.

    Only ``https://`` URLs are accepted. Credentials embedded in a URL are
    refused because they would end up written to ``.git/config`` inside a
    workspace an agent can read.
    """
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in {"https"}:
        raise UnprocessableError("Only https:// repository URLs are supported")
    if parsed.username or parsed.password:
        raise UnprocessableError("Repository URLs must not embed credentials")

    git = shutil.which("git")
    if git is None:
        raise UnprocessableError("git is not installed on this host")

    workspace_path = Path(project.workspace_path)
    target = workspace_path / (name or _repo_name(url))
    root_resolved = await asyncio.to_thread(workspace_path.resolve)
    target_resolved = await asyncio.to_thread(target.resolve)
    if target_resolved.parent != root_resolved:
        raise UnprocessableError("Repository name escapes the project workspace")
    if await asyncio.to_thread(target.exists):
        raise ConflictError(f"'{target.name}' already exists in the workspace")

    argv = [git, "clone", "--depth", "1"]
    if branch:
        argv.extend(["--branch", branch])
    argv.extend([url, str(target)])

    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(workspace_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except TimeoutError:
        process.kill()
        await process.communicate()
        raise UnprocessableError(f"Cloning exceeded {timeout_seconds}s") from None

    if process.returncode != 0:
        message = stderr.decode("utf-8", errors="replace").strip()[:500]
        raise UnprocessableError(f"git clone failed: {message}")
    return target.name


def _repo_name(url: str) -> str:
    cleaned = url.rstrip("/")
    name = cleaned.rsplit("/", 1)[-1]
    if name.endswith(".git"):
        name = name[:-4]
    return name or "repository"


async def record_coding_session(
    db: AsyncSession,
    *,
    project: Project,
    result: CodingResult,
    instruction: str,
    model: str | None = None,
    task_id: UUID | None = None,
    agent_id: UUID | None = None,
    requested_by: UUID | None = None,
) -> CodingSession:
    """Persist a coding-engine run, including the preserved transcript path."""
    status = (
        CodingSessionStatus.TIMED_OUT
        if result.error and "timed out" in result.error
        else CodingSessionStatus.COMPLETED
        if result.ok
        else CodingSessionStatus.FAILED
    )
    session = CodingSession(
        project_id=project.id,
        task_id=task_id,
        agent_id=agent_id,
        requested_by=requested_by,
        engine=result.engine,
        model=model,
        session_id=result.session_id,
        status=status,
        instruction=instruction,
        summary=result.summary,
        transcript_path=result.transcript_path,
        exit_code=result.exit_code,
        duration_ms=result.duration_ms,
        changes=result.changes,
        commands=result.commands,
        error=result.error,
        finished_at=datetime.now(UTC),
    )
    db.add(session)
    await db.flush()
    return session
