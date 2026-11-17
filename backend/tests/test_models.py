"""Integration test that exercises every table through the ORM."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.db.enums import (
    ApprovalAction,
    BountyPlatform,
    CompetitionStatus,
    FindingCategory,
    FindingStatus,
    MemoryLayer,
    MessageRole,
    ProviderType,
    Severity,
    TaskDependencyType,
    TaskStatus,
    UserRole,
)
from app.db.models import (
    Agent,
    AgentConfig,
    AgentMessage,
    AgentSkill,
    AgentTool,
    Approval,
    AuditLog,
    BountyProgram,
    Competition,
    Conversation,
    ConversationMessage,
    Evidence,
    Finding,
    Memory,
    ModelProvider,
    ModelUsage,
    Project,
    Repository,
    Skill,
    SystemSetting,
    Task,
    TaskDependency,
    TaskEvent,
    TaskStep,
    ToolRegistry,
    User,
)
from app.services.users import create_user


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@pytest.mark.integration
async def test_full_relational_graph_persists(db_session) -> None:
    db = db_session
    owner = await create_user(
        db,
        email="graph@example.com",
        password="Str0ng-Passw0rd!",
        role=UserRole.OWNER,
    )

    provider = ModelProvider(
        slug="openai-main",
        name="OpenAI",
        provider_type=ProviderType.OPENAI,
        default_model="gpt-4.1",
        api_key_encrypted="ciphertext",
        api_key_hint="...abcd",
        enabled=True,
        created_by=owner.id,
    )
    tool = ToolRegistry(
        slug="terminal",
        name="Terminal",
        description="Execute commands in a sandbox",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        required_permissions=["exec"],
        enabled=True,
    )
    skill = Skill(
        slug="repo-analysis",
        name="Repository analysis",
        description="Map a repository",
        version="1.0.0",
        entry_path="/skills/repo-analysis/SKILL.md",
        directory_path="/skills/repo-analysis",
        frontmatter={"name": "repo-analysis"},
        content_hash=_hash("skill"),
    )
    db.add_all([provider, tool, skill])
    await db.flush()

    agent = Agent(
        slug="orchestrator",
        name="Main Orchestrator",
        description="Coordinates tasks",
        system_prompt="You are the orchestrator.",
        model_provider_id=provider.id,
        model_name="gpt-4.1",
        status="active",
        is_system=True,
        created_by=owner.id,
        permissions={"network": False},
        memory_config={"layers": ["working", "semantic"]},
    )
    db.add(agent)
    await db.flush()

    db.add_all(
        [
            AgentConfig(agent_id=agent.id, key="planning", value={"depth": 2}),
            AgentTool(agent_id=agent.id, tool_id=tool.id),
            AgentSkill(agent_id=agent.id, skill_id=skill.id),
        ]
    )

    project = Project(
        slug="demo",
        name="Demo",
        description="Demo project",
        owner_id=owner.id,
        workspace_path="/data/workspaces/demo",
    )
    db.add(project)
    await db.flush()

    repository = Repository(
        project_id=project.id,
        name="demo",
        url="https://github.com/example/demo",
        local_path="/data/workspaces/demo/repo",
    )
    program = BountyProgram(
        slug="make-fun",
        name="Make.fun",
        platform=BountyPlatform.MAKE_FUN,
        url="https://make.fun",
        scope={"type": "platform"},
    )
    db.add_all([repository, program])
    await db.flush()

    competition = Competition(
        program_id=program.id,
        project_id=project.id,
        external_id="comp-1",
        title="Build an agent",
        url="https://make.fun/competitions/1",
        requirements={"stack": ["python"]},
        reward_amount=Decimal("1000.00"),
        status=CompetitionStatus.IN_PROGRESS,
        deadline_at=datetime.now(UTC) + timedelta(days=7),
    )
    db.add(competition)
    await db.flush()

    task = Task(
        project_id=project.id,
        created_by=owner.id,
        assigned_agent_id=agent.id,
        title="Analyse repository",
        objective="Produce an architecture report",
        status=TaskStatus.RUNNING,
        priority=10,
        input={"repo": "demo"},
        budget_usd=Decimal("2.500000"),
        correlation_id=None,
    )
    db.add(task)
    await db.flush()

    dependent = Task(
        project_id=project.id,
        parent_task_id=task.id,
        title="Write report",
        objective="Summarise findings",
    )
    db.add(dependent)
    await db.flush()

    step = TaskStep(
        task_id=task.id,
        agent_id=agent.id,
        seq=1,
        kind="tool",
        status="completed",
        title="Clone repository",
        input={"url": repository.url},
        output={"ok": True},
    )
    db.add(step)
    await db.flush()

    db.add_all(
        [
            TaskDependency(
                task_id=dependent.id,
                depends_on_task_id=task.id,
                dependency_type=TaskDependencyType.FINISH_TO_START,
            ),
            TaskEvent(
                task_id=task.id,
                step_id=step.id,
                agent_id=agent.id,
                event_type="tool.completed",
                message="Cloned",
                payload={"duration_ms": 42},
            ),
            AgentMessage(
                task_id=task.id,
                from_agent_id=agent.id,
                to_agent_id=agent.id,
                message_type="handoff",
                content="Repository ready",
            ),
            Conversation(
                agent_id=agent.id,
                task_id=task.id,
                user_id=owner.id,
                title="Session",
                last_message_at=datetime.now(UTC),
            ),
        ]
    )
    await db.flush()

    conversation = (
        await db.execute(select(Conversation).where(Conversation.task_id == task.id))
    ).scalar_one()
    db.add(
        ConversationMessage(
            conversation_id=conversation.id,
            role=MessageRole.ASSISTANT,
            content="Starting analysis",
            tool_calls=[{"id": "call-1", "name": "terminal"}],
            token_count=12,
        )
    )

    finding = Finding(
        project_id=project.id,
        competition_id=competition.id,
        task_id=task.id,
        agent_id=agent.id,
        created_by=owner.id,
        title="Missing authorization check",
        category=FindingCategory.API,
        description="Endpoint allows cross-tenant reads",
        hypothesis="Object id is not scoped to tenant",
        severity=Severity.HIGH,
        status=FindingStatus.CONFIRMED,
        confidence=0.9,
        cvss_score=Decimal("8.1"),
        cwe_id="CWE-639",
        remediation="Scope queries by tenant id",
        evidence_summary="Reproduced locally",
    )
    db.add(finding)
    await db.flush()

    db.add(
        Evidence(
            finding_id=finding.id,
            kind="http",
            title="Request",
            content="GET /api/items/123",
            content_hash=_hash("evidence"),
            meta={"status": 200},
        )
    )
    db.add(
        Approval(
            task_id=task.id,
            step_id=step.id,
            finding_id=finding.id,
            requested_by_agent_id=agent.id,
            assigned_to_user_id=owner.id,
            action_type=ApprovalAction.EXTERNAL_REPORT,
            title="Submit finding",
            description="Send to program",
            payload={"program": "make-fun"},
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
    )
    db.add(
        Memory(
            agent_id=agent.id,
            project_id=project.id,
            task_id=task.id,
            user_id=owner.id,
            layer=MemoryLayer.PROJECT,
            key="architecture",
            content="Service uses FastAPI and Postgres",
            embedding=[0.1, 0.2, 0.3],
            content_hash=_hash("memory"),
            importance=0.8,
        )
    )
    db.add(
        ModelUsage(
            provider_id=provider.id,
            agent_id=agent.id,
            task_id=task.id,
            model_name="gpt-4.1",
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
            cost_usd=Decimal("0.003500"),
            latency_ms=850,
        )
    )
    db.add(
        AuditLog(
            actor_user_id=owner.id,
            action="task.created",
            resource_type="task",
            resource_id=str(task.id),
            meta={"source": "test"},
        )
    )
    db.add(
        SystemSetting(
            key="platform.registration_enabled",
            value={"value": False},
            description="Registration toggle",
        )
    )

    await db.commit()

    expected_counts = {
        User: 1,
        ModelProvider: 1,
        ToolRegistry: 1,
        Skill: 1,
        Agent: 1,
        AgentConfig: 1,
        AgentTool: 1,
        AgentSkill: 1,
        Project: 1,
        Repository: 1,
        BountyProgram: 1,
        Competition: 1,
        Task: 2,
        TaskDependency: 1,
        TaskStep: 1,
        TaskEvent: 1,
        AgentMessage: 1,
        Conversation: 1,
        ConversationMessage: 1,
        Finding: 1,
        Evidence: 1,
        Approval: 1,
        Memory: 1,
        ModelUsage: 1,
        AuditLog: 1,
        SystemSetting: 1,
    }
    for model, expected in expected_counts.items():
        count = await db.scalar(select(func.count()).select_from(model))
        assert count == expected, f"{model.__name__} expected {expected}, found {count}"
