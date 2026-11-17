"""Skill management: install, catalogue, assign, search and record usage.

Skills are instructions, not code, so there is no interpreter to run. What this
service guarantees is that the metadata is valid, the body an agent receives is
exactly the reviewed file, and every load is recorded.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import ConflictError, NotFoundError, UnprocessableError
from app.db.models import Agent, AgentSkill, Skill, SkillExecution
from app.skills.loader import (
    SkillDocument,
    SkillError,
    copy_skill_directory,
    discover_skills,
    load_skill_directory,
    parse_skill_file,
    skill_prompt,
    validate_frontmatter,
)

logger = structlog.get_logger("agentos.skills")

BUILTIN_SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills" / "builtin"


@dataclass(slots=True)
class SyncReport:
    installed: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "installed": self.installed,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "failed": self.failed,
        }


async def get_skill(db: AsyncSession, skill_id: UUID) -> Skill:
    skill = await db.get(Skill, skill_id)
    if skill is None:
        raise NotFoundError("Skill not found")
    return skill


async def get_skill_by_slug(
    db: AsyncSession, slug: str, *, version: str | None = None, enabled_only: bool = False
) -> Skill:
    statement: Select[tuple[Skill]] = select(Skill).where(Skill.slug == slug)
    if version is not None:
        statement = statement.where(Skill.version == version)
    if enabled_only:
        statement = statement.where(Skill.enabled.is_(True))
    statement = statement.order_by(Skill.created_at.desc()).limit(1)
    skill = (await db.execute(statement)).scalar_one_or_none()
    if skill is None:
        raise NotFoundError(f"Skill '{slug}' is not installed")
    return skill


async def list_skills(
    db: AsyncSession,
    *,
    query: str | None = None,
    enabled: bool | None = None,
    source: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Skill], int]:
    statement: Select[tuple[Skill]] = select(Skill)
    if query:
        pattern = f"%{query.lower()}%"
        statement = statement.where(
            or_(
                func.lower(Skill.slug).like(pattern),
                func.lower(Skill.name).like(pattern),
                func.lower(Skill.description).like(pattern),
            )
        )
    if enabled is not None:
        statement = statement.where(Skill.enabled.is_(enabled))
    if source is not None:
        statement = statement.where(Skill.source == source)

    total = (await db.execute(select(func.count()).select_from(statement.subquery()))).scalar_one()
    rows = (
        (
            await db.execute(
                statement.order_by(Skill.slug, Skill.created_at.desc()).limit(limit).offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), int(total)


async def _upsert_document(
    db: AsyncSession,
    document: SkillDocument,
    *,
    source: str,
    is_system: bool,
    created_by: UUID | None,
    overwrite: bool,
) -> str:
    """Persist one parsed skill. Returns ``installed``/``updated``/``unchanged``."""
    existing = (
        await db.execute(
            select(Skill).where(Skill.slug == document.slug, Skill.version == document.version)
        )
    ).scalar_one_or_none()

    if existing is None:
        db.add(
            Skill(
                slug=document.slug,
                name=document.name,
                description=document.description,
                version=document.version,
                source=source,
                entry_path=document.entry_path,
                directory_path=document.directory_path,
                frontmatter=document.frontmatter,
                content_hash=document.content_hash,
                enabled=True,
                is_system=is_system,
                created_by=created_by,
            )
        )
        await db.flush()
        return "installed"

    if existing.content_hash == document.content_hash:
        # Keep the on-disk path fresh even when the content is identical: the
        # managed root can move between deployments.
        existing.directory_path = document.directory_path or existing.directory_path
        existing.frontmatter = document.frontmatter
        return "unchanged"

    if not overwrite:
        return "unchanged"

    existing.name = document.name
    existing.description = document.description
    existing.directory_path = document.directory_path or existing.directory_path
    existing.frontmatter = document.frontmatter
    existing.content_hash = document.content_hash
    await db.flush()
    return "updated"


async def sync_from_directory(
    db: AsyncSession,
    root: Path | None = None,
    *,
    source: str = "local",
    is_system: bool = False,
    created_by: UUID | None = None,
    overwrite: bool = True,
) -> SyncReport:
    """Install or refresh every SKILL.md found under ``root``."""
    settings = get_settings()
    directory = root or settings.skills_root
    report = SyncReport()

    # Directory walking and file parsing are blocking; keep them off the loop.
    discovered = await asyncio.to_thread(lambda: list(discover_skills(directory)))
    for path, error in discovered:
        if error is not None:
            report.failed[str(path)] = error.message
            continue
        try:
            document = await asyncio.to_thread(
                parse_skill_file, path, default_slug=path.parent.name
            )
            outcome = await _upsert_document(
                db,
                document,
                source=source,
                is_system=is_system,
                created_by=created_by,
                overwrite=overwrite,
            )
        except SkillError as exc:
            report.failed[str(path)] = exc.message
            continue
        getattr(report, outcome if outcome != "unchanged" else "unchanged").append(document.slug)
    return report


async def sync_builtin_skills(db: AsyncSession) -> SyncReport:
    """Copy the shipped skills into the managed root and register them.

    The copy means an operator can edit a built-in skill in the managed root
    without it being overwritten on the next boot, while the shipped version
    remains the source of truth for new installs.
    """
    settings = get_settings()
    await asyncio.to_thread(settings.skills_root.mkdir, parents=True, exist_ok=True)
    copied: list[Path] = []
    failed: dict[str, str] = {}

    for entry in sorted(await asyncio.to_thread(lambda: list(BUILTIN_SKILLS_DIR.iterdir()))):
        if not await asyncio.to_thread(entry.is_dir):
            continue
        try:
            await asyncio.to_thread(load_skill_directory, entry)
        except SkillError as exc:
            failed[entry.name] = exc.message
            continue
        copied.append(
            await asyncio.to_thread(copy_skill_directory, entry, settings.skills_root, entry.name)
        )

    report = await sync_from_directory(
        db, settings.skills_root, source="builtin", is_system=True, overwrite=False
    )
    report.failed.update(failed)
    logger.info(
        "builtin_skills_synced",
        installed=len(report.installed),
        unchanged=len(report.unchanged),
        failed=len(report.failed),
        directories=len(copied),
    )
    return report


async def install_from_path(
    db: AsyncSession,
    path: Path,
    *,
    source: str = "local",
    created_by: UUID | None = None,
    overwrite: bool = True,
) -> Skill:
    """Install a skill from a directory (or a SKILL.md file) outside the root."""
    settings = get_settings()
    is_directory = await asyncio.to_thread(path.is_dir)
    entry = path / "SKILL.md" if is_directory else path
    try:
        document = await asyncio.to_thread(parse_skill_file, entry, default_slug=entry.parent.name)
    except SkillError as exc:
        raise UnprocessableError(exc.message, details=exc.details) from exc

    if is_directory:
        target = await asyncio.to_thread(
            copy_skill_directory, path, settings.skills_root, document.slug
        )
        document = await asyncio.to_thread(
            parse_skill_file, target / "SKILL.md", default_slug=document.slug
        )

    await _upsert_document(
        db,
        document,
        source=source,
        is_system=False,
        created_by=created_by,
        overwrite=overwrite,
    )
    return await get_skill_by_slug(db, document.slug, version=document.version)


async def create_custom_skill(
    db: AsyncSession,
    *,
    slug: str,
    name: str,
    description: str,
    version: str,
    body: str,
    frontmatter: dict[str, Any] | None = None,
    enabled: bool = True,
    created_by: UUID | None = None,
) -> Skill:
    """Write a new SKILL.md under the managed root and register it."""
    import yaml

    settings = get_settings()
    target = settings.skills_root / slug
    if await asyncio.to_thread(target.exists):
        raise ConflictError(f"A skill directory named '{slug}' already exists")

    metadata = {
        "slug": slug,
        "name": name,
        "description": description,
        "version": version,
        **(frontmatter or {}),
    }
    metadata.pop("slug", None)
    metadata = {"slug": slug, **metadata}
    try:
        validate_frontmatter(metadata)
    except SkillError as exc:
        raise UnprocessableError(exc.message, details=exc.details) from exc

    def _write() -> None:
        target.mkdir(parents=True, exist_ok=False)
        rendered = (
            f"---\n{yaml.safe_dump(metadata, sort_keys=False).strip()}\n---\n\n{body.strip()}\n"
        )
        (target / "SKILL.md").write_text(rendered, encoding="utf-8")

    await asyncio.to_thread(_write)
    await install_from_path(db, target, source="custom", created_by=created_by, overwrite=True)
    skill = await get_skill_by_slug(db, slug, version=version)
    if not enabled:
        await set_skill_enabled(db, skill, False)
    return skill


async def list_skill_files(skill: Skill) -> list[str]:
    """Relative paths of every file that ships with a skill."""
    directory = Path(skill.directory_path)

    def _walk() -> list[str]:
        if not directory.is_dir():
            return []
        return sorted(
            str(path.relative_to(directory)) for path in directory.rglob("*") if path.is_file()
        )

    return await asyncio.to_thread(_walk)


async def assigned_agent_ids(db: AsyncSession, skill_id: UUID) -> list[UUID]:
    rows = await db.execute(select(AgentSkill.agent_id).where(AgentSkill.skill_id == skill_id))
    return list(rows.scalars())


async def install_from_git(
    db: AsyncSession,
    repo_url: str,
    *,
    ref: str | None = None,
    subdirectory: str | None = None,
    created_by: UUID | None = None,
    overwrite: bool = True,
) -> list[Skill]:
    """Clone a repository and install every SKILL.md it contains.

    ``git`` is invoked without a shell and with a timeout, so a hostile URL
    cannot smuggle options or hang the request.
    """
    settings = get_settings()
    if not repo_url.startswith(("https://", "http://", "git@", "ssh://")):
        raise UnprocessableError("Skill repository URL must be an https, ssh or git URL")
    if repo_url.startswith("-"):
        raise UnprocessableError("Skill repository URL is not valid")

    with tempfile.TemporaryDirectory(prefix="agentos-skill-") as workspace:
        target = Path(workspace) / "repo"
        command = ["git", "clone", "--depth", "1"]
        if ref:
            command += ["--branch", ref]
        command += [repo_url, str(target)]

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={"GIT_TERMINAL_PROMPT": "0", "PATH": "/usr/local/bin:/usr/bin:/bin"},
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=settings.skill_git_timeout_seconds
                )
            except TimeoutError as exc:
                process.kill()
                await process.communicate()
                raise UnprocessableError("Cloning the skill repository timed out") from exc
        except FileNotFoundError as exc:
            raise UnprocessableError("git is not available in this environment") from exc

        if process.returncode != 0:
            detail = (stderr or stdout).decode("utf-8", "replace").strip()[:500]
            raise UnprocessableError(f"git clone failed: {detail or 'unknown error'}")

        search_root = target
        if subdirectory:
            candidate = (target / subdirectory).resolve()
            try:
                candidate.relative_to(target.resolve())
            except ValueError as exc:
                raise UnprocessableError("Subdirectory escapes the repository") from exc
            if not candidate.exists():
                raise UnprocessableError(f"Subdirectory '{subdirectory}' does not exist")
            search_root = candidate

        documents: list[SkillDocument] = []
        discovered_files = await asyncio.to_thread(lambda: list(discover_skills(search_root)))
        for skill_file, error in discovered_files:
            if error is not None:
                logger.warning("skill_import_skipped", path=str(skill_file), error=error.message)
                continue
            try:
                documents.append(
                    await asyncio.to_thread(
                        parse_skill_file, skill_file, default_slug=skill_file.parent.name
                    )
                )
            except SkillError as exc:
                logger.warning("skill_import_skipped", path=str(skill_file), error=exc.message)

        if not documents:
            raise UnprocessableError("No valid SKILL.md was found in the repository")

        installed: list[Skill] = []
        for document in documents:
            source_dir = Path(document.directory_path)
            target_dir = await asyncio.to_thread(
                copy_skill_directory, source_dir, settings.skills_root, document.slug
            )
            stored = await asyncio.to_thread(
                parse_skill_file, target_dir / "SKILL.md", default_slug=document.slug
            )
            await _upsert_document(
                db,
                stored,
                source="git",
                is_system=False,
                created_by=created_by,
                overwrite=overwrite,
            )
            installed.append(await get_skill_by_slug(db, stored.slug, version=stored.version))
        return installed


async def set_skill_enabled(db: AsyncSession, skill: Skill, enabled: bool) -> Skill:
    skill.enabled = enabled
    await db.flush()
    return skill


async def delete_skill(db: AsyncSession, skill: Skill, *, remove_files: bool = True) -> None:
    if skill.is_system:
        raise ConflictError("Built-in skills cannot be deleted; disable them instead")
    directory = Path(skill.directory_path) if skill.directory_path else None
    await db.delete(skill)
    await db.flush()
    if remove_files and directory is not None and await asyncio.to_thread(directory.is_dir):
        await asyncio.to_thread(shutil.rmtree, directory, True)


# ------------------------------------------------------------------ assignment
async def assign_skill(
    db: AsyncSession, agent: Agent, skill: Skill, *, enabled: bool = True
) -> AgentSkill:
    existing = (
        await db.execute(
            select(AgentSkill).where(
                AgentSkill.agent_id == agent.id, AgentSkill.skill_id == skill.id
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.enabled = enabled
        await db.flush()
        return existing
    assignment = AgentSkill(agent_id=agent.id, skill_id=skill.id, enabled=enabled, config={})
    db.add(assignment)
    await db.flush()
    return assignment


async def unassign_skill(db: AsyncSession, agent: Agent, skill: Skill) -> None:
    assignment = (
        await db.execute(
            select(AgentSkill).where(
                AgentSkill.agent_id == agent.id, AgentSkill.skill_id == skill.id
            )
        )
    ).scalar_one_or_none()
    if assignment is None:
        raise NotFoundError("Skill is not assigned to this agent")
    await db.delete(assignment)
    await db.flush()


async def agent_skills(
    db: AsyncSession, agent_id: UUID, *, enabled_only: bool = True
) -> list[Skill]:
    statement = (
        select(Skill)
        .join(AgentSkill, AgentSkill.skill_id == Skill.id)
        .where(AgentSkill.agent_id == agent_id)
    )
    if enabled_only:
        statement = statement.where(AgentSkill.enabled.is_(True), Skill.enabled.is_(True))
    return list((await db.execute(statement.order_by(Skill.slug))).scalars().all())


async def load_skill_body(db: AsyncSession, skill: Skill) -> str:
    """Read a skill's body from disk, verifying it has not changed underneath us."""
    entry = Path(skill.directory_path) / skill.entry_path
    if not await asyncio.to_thread(entry.is_file):
        raise NotFoundError(f"Skill '{skill.slug}' files are missing from disk")
    try:
        document = await asyncio.to_thread(parse_skill_file, entry, default_slug=skill.slug)
    except SkillError as exc:
        raise UnprocessableError(f"Skill '{skill.slug}' is no longer valid: {exc.message}") from exc
    if document.content_hash != skill.content_hash:
        raise ConflictError(
            f"Skill '{skill.slug}' changed on disk; re-run the skill sync before loading it"
        )
    return skill_prompt(document)


async def record_execution(
    db: AsyncSession,
    skill: Skill,
    *,
    event: str = "load",
    ok: bool = True,
    detail: str = "",
    agent_id: UUID | None = None,
    task_id: UUID | None = None,
    duration_ms: int = 0,
    meta: dict[str, Any] | None = None,
) -> SkillExecution:
    execution = SkillExecution(
        skill_id=skill.id,
        agent_id=agent_id,
        task_id=task_id,
        event=event,
        ok=ok,
        detail=detail[:2000],
        duration_ms=duration_ms,
        meta=meta or {},
    )
    db.add(execution)
    await db.flush()
    return execution


async def execution_history(
    db: AsyncSession, *, skill_id: UUID | None = None, task_id: UUID | None = None, limit: int = 50
) -> list[SkillExecution]:
    statement = select(SkillExecution)
    if skill_id is not None:
        statement = statement.where(SkillExecution.skill_id == skill_id)
    if task_id is not None:
        statement = statement.where(SkillExecution.task_id == task_id)
    return list(
        (await db.execute(statement.order_by(SkillExecution.created_at.desc()).limit(limit)))
        .scalars()
        .all()
    )


# ------------------------------------------------------------------- catalogue
async def catalogue_for_agent(db: AsyncSession, agent_id: UUID) -> list[dict[str, Any]]:
    """The metadata an agent sees up front — never the full instructions."""
    skills = await agent_skills(db, agent_id)
    return [
        {
            "slug": skill.slug,
            "name": skill.name,
            "description": skill.description,
            "version": skill.version,
        }
        for skill in skills
    ]


async def load_skill_for_agent(
    db: AsyncSession,
    *,
    agent_id: UUID,
    slug: str,
    task_id: UUID | None = None,
    record: bool = True,
) -> str:
    """Load one skill's instructions for an agent that has it assigned."""
    assigned = {skill.slug: skill for skill in await agent_skills(db, agent_id)}
    skill = assigned.get(slug)
    if skill is None:
        raise NotFoundError(f"Skill '{slug}' is not assigned to this agent")

    started = time.perf_counter()
    try:
        body = await load_skill_body(db, skill)
    except (NotFoundError, UnprocessableError, ConflictError) as exc:
        if record:
            await record_execution(
                db,
                skill,
                event="load",
                ok=False,
                detail=getattr(exc, "message", str(exc)),
                agent_id=agent_id,
                task_id=task_id,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
        raise
    if record:
        await record_execution(
            db,
            skill,
            event="load",
            ok=True,
            agent_id=agent_id,
            task_id=task_id,
            duration_ms=int((time.perf_counter() - started) * 1000),
            meta={"bytes": len(body)},
        )
    return body


async def skill_versions(db: AsyncSession, slug: str) -> list[Skill]:
    """Every stored version of one skill, newest version first."""
    rows = await db.execute(select(Skill).where(Skill.slug == slug).order_by(Skill.version.desc()))
    return list(rows.scalars().all())
