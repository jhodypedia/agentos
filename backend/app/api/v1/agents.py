"""Agent and tool endpoints."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Query, Request, status
from sqlalchemy import select

from app.api.deps import AdminPrincipal, DbSession, OperatorPrincipal, PrincipalDep
from app.core.exceptions import NotFoundError
from app.db.enums import AgentStatus, RiskLevel
from app.db.models import ToolRegistry
from app.schemas.agent import (
    AgentCreate,
    AgentDetail,
    AgentPublic,
    AgentSkillAssign,
    AgentToolAssign,
    AgentToolPublic,
    AgentUpdate,
    ToolExecutionRequest,
    ToolExecutionResponse,
    ToolPublic,
)
from app.services import agents as agent_service
from app.services.audit import record_audit
from app.tools.base import ToolContext
from app.tools.errors import (
    ApprovalRequiredError,
    ToolPermissionError,
    ToolValidationError,
)

router = APIRouter(prefix="/agents", tags=["agents"])
tools_router = APIRouter(prefix="/tools", tags=["tools"])


# ---------------------------------------------------------------------- agents
@router.get("", response_model=list[AgentPublic], summary="List agents")
async def list_agents(
    principal: OperatorPrincipal,
    db: DbSession,
    agent_status: AgentStatus | None = Query(default=None, alias="status"),
) -> list[AgentPublic]:
    agents = await agent_service.list_agents(db, status=agent_status)
    return [AgentPublic.model_validate(agent) for agent in agents]


@router.post(
    "",
    response_model=AgentDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Create an agent",
)
async def create_agent(
    payload: AgentCreate,
    principal: AdminPrincipal,
    db: DbSession,
    request: Request,
) -> AgentDetail:
    agent = await agent_service.create_agent(
        db,
        slug=payload.slug,
        name=payload.name,
        description=payload.description,
        system_prompt=payload.system_prompt,
        model_provider_id=payload.model_provider_id,
        model_name=payload.model_name,
        temperature=payload.temperature,
        top_p=payload.top_p,
        max_output_tokens=payload.max_output_tokens,
        max_iterations=payload.max_iterations,
        token_budget=payload.token_budget,
        timeout_seconds=payload.timeout_seconds,
        permissions=payload.permissions,
        memory_config=payload.memory_config,
        status=payload.status,
        created_by=principal.user.id,
    )
    await record_audit(
        db,
        action="agent.created",
        resource_type="agent",
        resource_id=agent.id,
        actor_user_id=principal.user.id,
        request=request,
        meta={"slug": agent.slug, "model": agent.model_name},
    )
    await db.commit()
    agent = await agent_service.get_agent(db, agent.id)
    return AgentDetail.model_validate(agent)


@router.get("/{agent_id}", response_model=AgentDetail, summary="Get an agent")
async def get_agent(agent_id: UUID, principal: OperatorPrincipal, db: DbSession) -> AgentDetail:
    agent = await agent_service.get_agent(db, agent_id)
    return AgentDetail.model_validate(agent)


@router.patch("/{agent_id}", response_model=AgentDetail, summary="Update an agent")
async def update_agent(
    agent_id: UUID,
    payload: AgentUpdate,
    principal: AdminPrincipal,
    db: DbSession,
    request: Request,
) -> AgentDetail:
    agent = await agent_service.get_agent(db, agent_id)
    fields = payload.model_dump(exclude_unset=True)
    await agent_service.update_agent(db, agent, **fields)
    await record_audit(
        db,
        action="agent.updated",
        resource_type="agent",
        resource_id=agent.id,
        actor_user_id=principal.user.id,
        request=request,
        meta={"fields": sorted(fields)},
    )
    await db.commit()
    agent = await agent_service.get_agent(db, agent_id)
    return AgentDetail.model_validate(agent)


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete an agent")
async def delete_agent(
    agent_id: UUID,
    principal: AdminPrincipal,
    db: DbSession,
    request: Request,
) -> None:
    agent = await agent_service.get_agent(db, agent_id)
    slug = agent.slug
    await agent_service.delete_agent(db, agent)
    await record_audit(
        db,
        action="agent.deleted",
        resource_type="agent",
        resource_id=agent_id,
        actor_user_id=principal.user.id,
        request=request,
        meta={"slug": slug},
    )
    await db.commit()


@router.post(
    "/{agent_id}/tools",
    response_model=AgentToolPublic,
    summary="Assign or update a tool on an agent",
)
async def assign_tool(
    agent_id: UUID,
    payload: AgentToolAssign,
    principal: AdminPrincipal,
    db: DbSession,
    request: Request,
) -> AgentToolPublic:
    agent = await agent_service.get_agent(db, agent_id)
    assignment = await agent_service.assign_tool(
        db,
        agent,
        tool_slug=payload.tool_slug,
        enabled=payload.enabled,
        config=payload.config,
    )
    await record_audit(
        db,
        action="agent.tool_assigned",
        resource_type="agent",
        resource_id=agent.id,
        actor_user_id=principal.user.id,
        request=request,
        meta={"tool": payload.tool_slug, "enabled": payload.enabled},
    )
    await db.commit()
    await db.refresh(assignment)
    return AgentToolPublic.model_validate(assignment)


@router.delete(
    "/{agent_id}/tools/{tool_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a tool from an agent",
)
async def remove_tool(
    agent_id: UUID,
    tool_id: UUID,
    principal: AdminPrincipal,
    db: DbSession,
    request: Request,
) -> None:
    agent = await agent_service.get_agent(db, agent_id)
    for assignment in list(agent.tools):
        if assignment.tool_id == tool_id:
            await db.delete(assignment)
            await record_audit(
                db,
                action="agent.tool_removed",
                resource_type="agent",
                resource_id=agent.id,
                actor_user_id=principal.user.id,
                request=request,
                meta={"tool_id": str(tool_id)},
            )
            await db.commit()
            return
    raise NotFoundError("Tool assignment not found")


@router.post(
    "/{agent_id}/skills",
    status_code=status.HTTP_201_CREATED,
    summary="Assign a skill to an agent",
)
async def assign_skill(
    agent_id: UUID,
    payload: AgentSkillAssign,
    principal: AdminPrincipal,
    db: DbSession,
    request: Request,
) -> dict[str, str]:
    agent = await agent_service.get_agent(db, agent_id)
    assignment = await agent_service.assign_skill(
        db, agent, skill_id=payload.skill_id, enabled=payload.enabled
    )
    await record_audit(
        db,
        action="agent.skill_assigned",
        resource_type="agent",
        resource_id=agent.id,
        actor_user_id=principal.user.id,
        request=request,
        meta={"skill_id": str(payload.skill_id)},
    )
    await db.commit()
    return {"id": str(assignment.id)}


# ----------------------------------------------------------------------- tools
@tools_router.get("", response_model=list[ToolPublic], summary="List registered tools")
async def list_tools(principal: OperatorPrincipal, db: DbSession) -> list[ToolPublic]:
    result = await db.execute(select(ToolRegistry).order_by(ToolRegistry.slug))
    return [ToolPublic.model_validate(item) for item in result.scalars()]


@tools_router.post(
    "/{tool_slug}/execute",
    response_model=ToolExecutionResponse,
    summary="Execute a tool directly (validated, permission-checked)",
)
async def execute_tool(
    tool_slug: str,
    payload: ToolExecutionRequest,
    principal: PrincipalDep,
    db: DbSession,
    request: Request,
) -> ToolExecutionResponse:
    from app.core.config import get_settings
    from app.tools.builtin import default_tool_registry

    registry = default_tool_registry()
    tool = registry.get(tool_slug)
    settings = get_settings()

    permissions = {"*"} if principal.role.value in {"owner", "admin"} else set()
    context = ToolContext(
        workspace_root=str(settings.workspace_root),
        permissions=frozenset(permissions),
        approvals=frozenset(),
    )
    try:
        result = await tool.run(payload.arguments, context)
    except ToolValidationError as exc:
        await record_audit(
            db,
            action="tool.rejected",
            resource_type="tool",
            resource_id=tool_slug,
            actor_user_id=principal.user.id,
            request=request,
            success=False,
            meta={"reason": "validation", "error": exc.message},
        )
        await db.commit()
        return ToolExecutionResponse(ok=False, error=exc.message, duration_ms=0)
    except (ToolPermissionError, ApprovalRequiredError) as exc:
        await record_audit(
            db,
            action="tool.rejected",
            resource_type="tool",
            resource_id=tool_slug,
            actor_user_id=principal.user.id,
            request=request,
            success=False,
            meta={"reason": exc.code},
        )
        await db.commit()
        return ToolExecutionResponse(ok=False, error=exc.message, duration_ms=0)

    await record_audit(
        db,
        action="tool.executed",
        resource_type="tool",
        resource_id=tool_slug,
        actor_user_id=principal.user.id,
        request=request,
        success=result.ok,
        meta={"risk_level": tool.risk_level.value, "duration_ms": result.duration_ms},
    )
    await db.commit()
    return ToolExecutionResponse(
        ok=result.ok,
        output=result.output,
        error=result.error,
        duration_ms=result.duration_ms,
    )


@tools_router.get("/risk-levels", response_model=list[str], summary="Supported tool risk levels")
async def risk_levels(principal: PrincipalDep) -> list[str]:
    return [level.value for level in RiskLevel]
