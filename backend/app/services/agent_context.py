"""Assemble the per-run context an agent receives: skills and memory.

Two rules shape this module:

* **Skills are advertised, not injected.** The model sees a one-line catalogue
  and calls ``load_skill`` when it needs the procedure, so a specialist with ten
  skills does not pay for ten bodies on every run.
* **Memory is retrieved for the current objective only.** The query is the
  task's title and objective, and the result is truncated to a character budget
  so a large memory table cannot crowd out the instructions.

Tool resources are closures over a session factory: a tool call opens its own
short transaction instead of borrowing the request's session, which would keep
a transaction open for the whole model run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.core.exceptions import NotFoundError
from app.db.enums import MemoryLayer
from app.db.models import Agent, Project, Task
from app.services import memory as memory_service
from app.services import skills as skill_service
from app.workspace.paths import Workspace, WorkspaceError

logger = structlog.get_logger("agentos.agent_context")

SKILL_TOOL_SLUGS = ("load_skill", "recall", "remember")


async def skill_catalogue_text(db: AsyncSession, agent: Agent) -> str:
    entries = await skill_service.catalogue_for_agent(db, agent.id)
    if not entries:
        return ""
    lines = [
        "## Available skills",
        "",
        "Each skill holds a procedure for a kind of work. Call `load_skill` with a",
        "slug before starting work that one of them covers; do not guess its contents.",
        "",
    ]
    lines.extend(f"- `{entry['slug']}` — {entry['description']}" for entry in entries)
    return "\n".join(lines)


def memory_settings(agent: Agent) -> tuple[bool, list[str] | None]:
    """Read the agent's memory configuration.

    ``memory_config = {"enabled": false}`` disables memory entirely;
    ``{"layers": ["semantic", "project"]}`` restricts which layers are retrieved.
    Unknown layer names are ignored rather than erroring, so an old config does
    not break a run.
    """
    config = agent.memory_config or {}
    if not isinstance(config, dict):
        return True, None
    enabled = bool(config.get("enabled", True))
    layers = config.get("layers")
    if isinstance(layers, str):
        layers = [layers]
    if isinstance(layers, list) and layers:
        return enabled, [str(layer) for layer in layers]
    return enabled, None


async def build_extra_context(
    db: AsyncSession,
    *,
    agent: Agent,
    task: Task,
    query: str | None = None,
) -> str:
    """Compose the additional system context for a run."""
    parts: list[str] = []

    catalogue = await skill_catalogue_text(db, agent)
    if catalogue:
        parts.append(catalogue)

    enabled, layers = memory_settings(agent)
    if enabled:
        settings = get_settings()
        hits = await memory_service.retrieve(
            db,
            query=query or f"{task.title} {task.objective}",
            agent_id=agent.id,
            project_id=task.project_id,
            task_id=task.id,
            user_id=task.created_by,
            layers=None,
            limit=settings.memory_max_items,
        )
        if layers:
            hits = [hit for hit in hits if hit.memory.layer.value in layers]
        if hits:
            lines = ["## Relevant memory", ""]
            used = 0
            consumed = []
            for hit in hits:
                body = (hit.memory.summary or hit.memory.content).strip()
                entry = f"- ({hit.memory.layer.value}) {body}"
                if used + len(entry) > settings.memory_max_context_chars:
                    break
                lines.append(entry)
                used += len(entry)
                consumed.append(hit.memory)
            if consumed:
                await memory_service.mark_accessed(db, consumed)
                parts.append("\n".join(lines))

    return "\n\n".join(parts)


def tool_resources(
    factory: async_sessionmaker[AsyncSession],
    *,
    agent_id: UUID,
    task_id: UUID,
    project_id: UUID | None,
    user_id: UUID | None,
    memory_enabled: bool = True,
) -> dict[str, Any]:
    """Build the callables the skill/memory tools invoke."""

    async def load_skill(slug: str) -> str:
        async with factory() as session:
            body = await skill_service.load_skill_for_agent(
                session, agent_id=agent_id, slug=slug, task_id=task_id
            )
            await session.commit()
            return body

    async def remember(payload: dict[str, Any]) -> dict[str, Any]:
        if not memory_enabled:
            raise RuntimeError("Memory is disabled for this agent")
        async with factory() as session:
            memory = await memory_service.write_memory(
                session,
                memory_service.MemoryWrite(
                    content=str(payload["content"]),
                    layer=MemoryLayer(str(payload.get("layer") or "semantic")),
                    agent_id=agent_id,
                    project_id=project_id,
                    task_id=task_id,
                    user_id=user_id,
                    key=payload.get("key"),
                    importance=float(payload.get("importance", 0.5)),
                    source="agent",
                    source_ref=str(task_id),
                ),
            )
            await session.commit()
            return {
                "id": str(memory.id),
                "layer": memory.layer.value,
                "version": memory.version,
                "stored": True,
            }

    async def recall(query: str, limit: int = 5) -> list[dict[str, Any]]:
        if not memory_enabled:
            return []
        async with factory() as session:
            hits = await memory_service.retrieve(
                session,
                query=query,
                agent_id=agent_id,
                project_id=project_id,
                task_id=task_id,
                user_id=user_id,
                limit=limit,
            )
            await memory_service.mark_accessed(session, [hit.memory for hit in hits])
            await session.commit()
            return [
                {
                    "layer": hit.memory.layer.value,
                    "content": (hit.memory.summary or hit.memory.content)[:600],
                    "score": round(hit.score, 3),
                }
                for hit in hits
            ]

    return {"load_skill": load_skill, "remember": remember, "recall": recall}


async def resolve_workspace(db: AsyncSession, project_id: UUID | None) -> Workspace | None:
    """Open the workspace a task operates in, or ``None`` when it has no project.

    A missing or moved directory is treated as "no workspace" rather than an
    error: the run should still start, and the filesystem tools will report the
    absence if the model tries to use them.
    """
    if project_id is None:
        return None
    project = await db.get(Project, project_id)
    if project is None:
        return None
    try:
        return await Workspace.open(Path(project.workspace_path))
    except (NotFoundError, WorkspaceError, OSError):
        return None
