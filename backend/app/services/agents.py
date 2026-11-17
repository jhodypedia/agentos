"""Agent definitions: creation, configuration and tool assignment."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.exceptions import ConflictError, NotFoundError, UnprocessableError
from app.db.enums import AgentStatus, RiskLevel
from app.db.models import Agent, AgentSkill, AgentTool, ModelProvider, Skill, ToolRegistry
from app.runtime.types import AgentSpec

MAX_ITERATIONS_LIMIT = 200
TOKEN_BUDGET_LIMIT = 20_000_000
TIMEOUT_LIMIT = 24 * 3600


async def list_agents(db: AsyncSession, *, status: AgentStatus | None = None) -> list[Agent]:
    statement = select(Agent).order_by(Agent.slug)
    if status is not None:
        statement = statement.where(Agent.status == status)
    result = await db.execute(statement)
    return list(result.scalars())


async def get_agent(db: AsyncSession, agent_id: UUID) -> Agent:
    statement = (
        select(Agent)
        .where(Agent.id == agent_id)
        .options(
            selectinload(Agent.tools),
            selectinload(Agent.skills),
            selectinload(Agent.configs),
        )
    )
    result = await db.execute(statement)
    agent = result.scalar_one_or_none()
    if agent is None:
        raise NotFoundError("Agent not found")
    return agent


async def get_agent_by_slug(db: AsyncSession, slug: str) -> Agent | None:
    result = await db.execute(select(Agent).where(Agent.slug == slug))
    return result.scalar_one_or_none()


def _validate_limits(
    *,
    max_iterations: int,
    token_budget: int,
    timeout_seconds: int,
    temperature: float,
    top_p: float,
    max_output_tokens: int,
) -> None:
    if not 1 <= max_iterations <= MAX_ITERATIONS_LIMIT:
        raise UnprocessableError(f"max_iterations must be between 1 and {MAX_ITERATIONS_LIMIT}")
    if not 1 <= token_budget <= TOKEN_BUDGET_LIMIT:
        raise UnprocessableError(f"token_budget must be between 1 and {TOKEN_BUDGET_LIMIT}")
    if not 1 <= timeout_seconds <= TIMEOUT_LIMIT:
        raise UnprocessableError(f"timeout_seconds must be between 1 and {TIMEOUT_LIMIT}")
    if not 0.0 <= temperature <= 2.0:
        raise UnprocessableError("temperature must be between 0 and 2")
    if not 0.0 < top_p <= 1.0:
        raise UnprocessableError("top_p must be greater than 0 and at most 1")
    if not 1 <= max_output_tokens <= 100_000:
        raise UnprocessableError("max_output_tokens must be between 1 and 100000")


async def create_agent(
    db: AsyncSession,
    *,
    slug: str,
    name: str,
    description: str = "",
    system_prompt: str = "",
    model_provider_id: UUID | None = None,
    model_name: str | None = None,
    temperature: float = 0.2,
    top_p: float = 1.0,
    max_output_tokens: int = 4096,
    max_iterations: int = 25,
    token_budget: int = 200_000,
    timeout_seconds: int = 1800,
    permissions: dict[str, Any] | None = None,
    memory_config: dict[str, Any] | None = None,
    status: AgentStatus = AgentStatus.ACTIVE,
    is_system: bool = False,
    created_by: UUID | None = None,
) -> Agent:
    normalised = slug.strip().lower()
    if not normalised:
        raise UnprocessableError("slug must not be empty")
    if await get_agent_by_slug(db, normalised) is not None:
        raise ConflictError(f"An agent with slug '{normalised}' already exists")

    _validate_limits(
        max_iterations=max_iterations,
        token_budget=token_budget,
        timeout_seconds=timeout_seconds,
        temperature=temperature,
        top_p=top_p,
        max_output_tokens=max_output_tokens,
    )

    if model_provider_id is not None:
        provider = await db.get(ModelProvider, model_provider_id)
        if provider is None:
            raise UnprocessableError("The selected model provider does not exist")
        if model_name is None:
            model_name = provider.default_model

    agent = Agent(
        slug=normalised,
        name=name.strip() or normalised,
        description=description,
        system_prompt=system_prompt,
        model_provider_id=model_provider_id,
        model_name=model_name,
        temperature=temperature,
        top_p=top_p,
        max_output_tokens=max_output_tokens,
        max_iterations=max_iterations,
        token_budget=token_budget,
        timeout_seconds=timeout_seconds,
        permissions=permissions or {"tools": []},
        memory_config=memory_config or {},
        status=status,
        is_system=is_system,
        created_by=created_by,
    )
    db.add(agent)
    await db.flush()
    return agent


async def update_agent(
    db: AsyncSession,
    agent: Agent,
    *,
    name: str | None = None,
    description: str | None = None,
    system_prompt: str | None = None,
    model_provider_id: UUID | None = None,
    model_name: str | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    max_output_tokens: int | None = None,
    max_iterations: int | None = None,
    token_budget: int | None = None,
    timeout_seconds: int | None = None,
    permissions: dict[str, Any] | None = None,
    memory_config: dict[str, Any] | None = None,
    status: AgentStatus | None = None,
) -> Agent:
    _validate_limits(
        max_iterations=max_iterations if max_iterations is not None else agent.max_iterations,
        token_budget=token_budget if token_budget is not None else agent.token_budget,
        timeout_seconds=(timeout_seconds if timeout_seconds is not None else agent.timeout_seconds),
        temperature=temperature if temperature is not None else agent.temperature,
        top_p=top_p if top_p is not None else agent.top_p,
        max_output_tokens=(
            max_output_tokens if max_output_tokens is not None else agent.max_output_tokens
        ),
    )

    if name is not None:
        agent.name = name.strip() or agent.name
    if description is not None:
        agent.description = description
    if system_prompt is not None:
        agent.system_prompt = system_prompt
    if model_provider_id is not None:
        if await db.get(ModelProvider, model_provider_id) is None:
            raise UnprocessableError("The selected model provider does not exist")
        agent.model_provider_id = model_provider_id
    if model_name is not None:
        agent.model_name = model_name or None
    if temperature is not None:
        agent.temperature = temperature
    if top_p is not None:
        agent.top_p = top_p
    if max_output_tokens is not None:
        agent.max_output_tokens = max_output_tokens
    if max_iterations is not None:
        agent.max_iterations = max_iterations
    if token_budget is not None:
        agent.token_budget = token_budget
    if timeout_seconds is not None:
        agent.timeout_seconds = timeout_seconds
    if permissions is not None:
        agent.permissions = permissions
    if memory_config is not None:
        agent.memory_config = memory_config
    if status is not None:
        agent.status = status

    agent.version += 1
    await db.flush()
    return agent


async def delete_agent(db: AsyncSession, agent: Agent) -> None:
    if agent.is_system:
        raise UnprocessableError("System agents cannot be deleted; archive them instead")
    await db.delete(agent)
    await db.flush()


async def assign_tool(
    db: AsyncSession,
    agent: Agent,
    *,
    tool_slug: str,
    enabled: bool = True,
    config: dict[str, Any] | None = None,
) -> AgentTool:
    tool_result = await db.execute(select(ToolRegistry).where(ToolRegistry.slug == tool_slug))
    tool = tool_result.scalar_one_or_none()
    if tool is None:
        raise UnprocessableError(f"Tool '{tool_slug}' is not registered")

    result = await db.execute(
        select(AgentTool).where(AgentTool.agent_id == agent.id, AgentTool.tool_id == tool.id)
    )
    assignment = result.scalar_one_or_none()
    if assignment is None:
        assignment = AgentTool(
            agent_id=agent.id,
            tool_id=tool.id,
            enabled=enabled,
            config=config or {},
        )
        db.add(assignment)
    else:
        assignment.enabled = enabled
        if config is not None:
            assignment.config = config
    try:
        await db.flush()
    except IntegrityError as exc:
        raise UnprocessableError("Tool assignment conflicts with an existing record") from exc
    return assignment


async def assign_skill(
    db: AsyncSession, agent: Agent, *, skill_id: UUID, enabled: bool = True
) -> AgentSkill:
    if await db.get(Skill, skill_id) is None:
        raise UnprocessableError("The selected skill does not exist")
    result = await db.execute(
        select(AgentSkill).where(AgentSkill.agent_id == agent.id, AgentSkill.skill_id == skill_id)
    )
    assignment = result.scalar_one_or_none()
    if assignment is None:
        assignment = AgentSkill(agent_id=agent.id, skill_id=skill_id, enabled=enabled)
        db.add(assignment)
    else:
        assignment.enabled = enabled
    await db.flush()
    return assignment


def _permissions_from_config(agent: Agent) -> frozenset[str]:
    raw = agent.permissions or {}
    tools = raw.get("tools", [])
    if isinstance(tools, str):
        tools = [tools]
    return frozenset(str(item) for item in tools if item) | {"tool:read"}


def build_agent_spec(
    agent: Agent, *, tool_slugs: set[str] | None = None, model_name: str | None = None
) -> AgentSpec:
    """Project a persisted agent into the runtime's immutable spec.

    ``model_name`` lets a caller run the agent on a different model (the
    orchestrator passes its own model down to specialists that have none).
    """
    resolved_model = model_name or agent.model_name
    if not resolved_model:
        raise UnprocessableError("The agent has no model configured")
    return AgentSpec(
        slug=agent.slug,
        name=agent.name,
        system_prompt=agent.system_prompt,
        model=resolved_model,
        temperature=agent.temperature,
        top_p=agent.top_p,
        max_output_tokens=agent.max_output_tokens,
        max_iterations=agent.max_iterations,
        token_budget=agent.token_budget,
        timeout_seconds=agent.timeout_seconds,
        permissions=_permissions_from_config(agent),
        tool_slugs=frozenset(tool_slugs) if tool_slugs is not None else None,
        metadata={"agent_status": agent.status.value},
    )


async def assigned_tool_slugs(db: AsyncSession, agent_id: UUID) -> set[str]:
    statement = (
        select(ToolRegistry.slug)
        .join(AgentTool, AgentTool.tool_id == ToolRegistry.id)
        .where(AgentTool.agent_id == agent_id, AgentTool.enabled.is_(True))
    )
    result = await db.execute(statement)
    return {str(row) for row in result.scalars()}


async def sync_tool_registry(db: AsyncSession, tools: list[Any]) -> dict[str, int]:
    """Upsert built-in tool declarations into ``tool_registry``.

    Enabled state is preserved for existing rows so an operator's decision is
    never silently reverted.
    """
    created = 0
    updated = 0
    for tool in tools:
        result = await db.execute(select(ToolRegistry).where(ToolRegistry.slug == tool.slug))
        record = result.scalar_one_or_none()
        declaration = tool.spec_dict()
        if record is None:
            db.add(
                ToolRegistry(
                    slug=declaration["slug"],
                    name=declaration["name"],
                    description=declaration["description"],
                    input_schema=declaration["parameters"],
                    output_schema={"type": "object"},
                    required_permissions=declaration["required_permissions"],
                    risk_level=RiskLevel(declaration["risk_level"]),
                    timeout_seconds=declaration["timeout_seconds"],
                    execution_environment=declaration["execution_environment"],
                    enabled=declaration["enabled"],
                    meta={"builtin": True, "requires_approval": declaration["requires_approval"]},
                )
            )
            created += 1
        else:
            record.name = declaration["name"]
            record.description = declaration["description"]
            record.input_schema = declaration["parameters"]
            record.required_permissions = declaration["required_permissions"]
            record.risk_level = RiskLevel(declaration["risk_level"])
            record.timeout_seconds = declaration["timeout_seconds"]
            record.execution_environment = declaration["execution_environment"]
            record.meta = {**(record.meta or {}), "builtin": True}
            updated += 1
    await db.flush()
    return {"created": created, "updated": updated}
