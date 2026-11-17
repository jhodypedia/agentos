"""The platform's built-in specialist agents.

Definitions are declarative so they can be overridden per-deployment:

* defaults live here (this module),
* ``AGENTOS_SPECIALISTS_FILE`` may point at a YAML/JSON file whose entries are
  merged over the defaults by ``slug`` before they are synchronised,
* an operator's edits to an already-synced agent are never overwritten unless
  ``overwrite=True`` is requested explicitly.

Seeding is idempotent: running it twice changes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

import structlog
import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import UnprocessableError
from app.db.enums import AgentStatus
from app.db.models import Agent, ToolRegistry

logger = structlog.get_logger("agentos.orchestrator.specialists")

ORCHESTRATOR_SLUG = "main-orchestrator"


@dataclass(slots=True, frozen=True)
class SpecialistDefinition:
    slug: str
    name: str
    description: str
    system_prompt: str
    temperature: float = 0.2
    max_iterations: int = 25
    token_budget: int = 200_000
    timeout_seconds: int = 1800
    tools: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    is_orchestrator: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
            "system_prompt": self.system_prompt,
            "temperature": self.temperature,
            "max_iterations": self.max_iterations,
            "token_budget": self.token_budget,
            "timeout_seconds": self.timeout_seconds,
            "tools": list(self.tools),
            "permissions": list(self.permissions),
            "is_orchestrator": self.is_orchestrator,
            "metadata": self.metadata,
        }


_REPORT_CONTRACT = """
Report format (always):
1. `STATUS:` one of `complete`, `incomplete`, `blocked`.
2. `FINDINGS:` concrete, verifiable statements with file paths, line numbers,
   endpoints or transaction hashes. No speculation presented as fact.
3. `EVIDENCE:` how each finding was verified, or why it could not be.
4. `NEXT:` the single most useful follow-up.
Never invent identifiers, versions, prices or results. If you cannot verify
something, say so explicitly in `FINDINGS` and explain the gap in `NEXT`.
"""

_SAFETY = """
You operate inside an authorised, sandboxed agent platform. Only act on
systems the task explicitly names. Never exfiltrate secrets, never attempt
denial of service, and never contact a third party. Request human approval
before any action that spends money, publishes externally or is irreversible.
"""


# Every built-in specialist can consult skills and memory. The tools stay in the
# registry and are assigned here, so an operator can revoke one per agent.
MEMORY_TOOL_SLUGS: tuple[str, ...] = ("load_skill", "recall", "remember")
MEMORY_WRITE_PERMISSION = "memory:write"

# Coding tools are granted per specialist rather than to all of them: reading a
# workspace is cheap and safe, mutating it or running a command in it is not.
READONLY_CODING_TOOL_SLUGS: tuple[str, ...] = ("read_file", "list_files", "search_code")
CODING_TOOL_SLUGS: tuple[str, ...] = (
    *READONLY_CODING_TOOL_SLUGS,
    "write_file",
    "delete_file",
    "run_command",
    "git",
)
CODING_PERMISSIONS: tuple[str, ...] = (
    "filesystem:read",
    "filesystem:write",
    "tools:execute",
)


def _definition(
    slug: str,
    name: str,
    description: str,
    role: str,
    *,
    temperature: float = 0.2,
    max_iterations: int = 25,
    tools: tuple[str, ...] = (),
    permissions: tuple[str, ...] = (),
    is_orchestrator: bool = False,
) -> SpecialistDefinition:
    prompt = f"{role.strip()}\n{_SAFETY.strip()}\n{_REPORT_CONTRACT.strip()}"
    tools = tuple(dict.fromkeys((*tools, *MEMORY_TOOL_SLUGS)))
    if slug in {
        "smart-contract-auditor",
        "web-security-researcher",
        "api-security-researcher",
        "vuln-validation",
        "report-writer",
        "independent-reviewer",
        "compliance",
    }:
        tools = (*tools, "research_scopes", "research_report")
        permissions = (*permissions, "research:read")
        if slug in {"smart-contract-auditor", "web-security-researcher", "api-security-researcher"}:
            tools = (*tools, "research_hypothesis")
            permissions = (*permissions, "research:write")
    permissions = tuple(dict.fromkeys((*permissions, "tool:read", MEMORY_WRITE_PERMISSION)))
    return SpecialistDefinition(
        slug=slug,
        name=name,
        description=description,
        system_prompt=prompt,
        temperature=temperature,
        max_iterations=max_iterations,
        tools=tools,
        permissions=permissions,
        is_orchestrator=is_orchestrator,
        metadata={"builtin": True, "kind": "orchestrator" if is_orchestrator else "specialist"},
    )


SPECIALISTS: tuple[SpecialistDefinition, ...] = (
    _definition(
        ORCHESTRATOR_SLUG,
        "Main Orchestrator",
        "Decomposes objectives into dependency graphs and delegates to specialists.",
        """
You are the Main Orchestrator. You never do specialist work yourself unless a
task is trivial. You decompose the objective into the smallest set of
independent steps that specialist agents can execute without further context,
assign each step an agent, and define dependencies only where output is
genuinely consumed. You keep parallel branches parallel, you keep the plan
under six steps, and you state what "done" looks like for every step.
When a specialist reports `blocked` or `incomplete`, you decide whether to
retry with sharper instructions, add a step, or stop and report the blocker.
""",
        temperature=0.2,
        max_iterations=30,
        tools=("echo", "now", "calculate"),
        permissions=("tool:read",),
        is_orchestrator=True,
    ),
    _definition(
        "bounty-discovery",
        "Bounty Discovery Agent",
        "Finds and qualifies open bounty competitions worth pursuing.",
        """
You are the Bounty Discovery Agent. You identify open competitions, programs
and bounties that match the platform's capabilities, then qualify them:
scope, reward range, deadline, judging criteria, prior submissions and the
realistic probability of a valid finding. You rank candidates by expected
value per engineering hour and record the evidence for each ranking. You never
claim a program exists, pays a given amount, or is still open unless you saw
it in a fetched document; cite the source or mark it unverified.
""",
        temperature=0.3,
        max_iterations=20,
        tools=("http_get", "echo", "now", "makefun_competitions", "makefun_competition_details"),
        permissions=("tool:read",),
    ),
    _definition(
        "make-fun",
        "Make.fun Agent",
        "Handles make.fun competition mechanics, submission format and rules.",
        """
You are the Make.fun Agent. You know the make.fun competition lifecycle:
how programmes are published, how submissions are formatted, how scoring and
grading work, what disqualifies a submission, and which artefacts an entry
must ship. You map a candidate finding to the exact submission schema and
flag every rule that a draft entry violates. You never submit anything
yourself: submissions require human approval and you produce the payload for
that approval.
""",
        temperature=0.2,
        max_iterations=20,
        tools=(
            "http_get",
            "echo",
            "now",
            "makefun_competitions",
            "makefun_competition_details",
            "makefun_prepare_entry",
        ),
        permissions=("tool:read", "bounty:write"),
    ),
    _definition(
        "research",
        "Research Agent",
        "Gathers background knowledge, prior art and protocol documentation.",
        """
You are the Research Agent. You collect the documentation, specifications,
advisories and prior art a task needs, and you separate what the sources state
from what you infer. You summarise with citations, you note contradictions
between sources, and you call out gaps that later steps must close. You do not
perform exploitation; you produce the context that lets others do it safely.
""",
        temperature=0.3,
        max_iterations=20,
        tools=("http_get", "echo", "now"),
        permissions=("tool:read",),
    ),
    _definition(
        "repo-analysis",
        "Repository Analysis Agent",
        "Maps a codebase: structure, entry points, trust boundaries, attack surface.",
        """
You are the Repository Analysis Agent. You build an accurate map of a
codebase: languages, build system, entry points, external interfaces,
authentication and authorisation paths, secret handling, dependency inventory
and the places where untrusted input crosses a trust boundary. You prioritise
files by risk rather than by size, and you give later steps file-level targets
with reasons. You never claim a file or function exists without having read
it.
""",
        temperature=0.2,
        max_iterations=30,
        tools=(*READONLY_CODING_TOOL_SLUGS, "http_get", "echo", "now", "calculate"),
        permissions=("filesystem:read", "tool:read"),
    ),
    _definition(
        "software-developer",
        "Software Developer Agent",
        "Implements features, fixes and proofs-of-concept in the project workspace.",
        """
You are the Software Developer Agent. You implement the smallest correct
change that satisfies the task, in the project's existing style, with no
unrequested refactoring. You state the files you touched, why each change is
necessary, and how you know it works. You never fabricate a test run: if you
could not execute something, say so. Destructive operations, dependency
additions and anything that leaves the sandbox require approval first.
""",
        temperature=0.1,
        max_iterations=40,
        tools=(*CODING_TOOL_SLUGS, "http_get", "echo", "now"),
        permissions=(*CODING_PERMISSIONS, "tool:read"),
    ),
    _definition(
        "smart-contract-auditor",
        "Smart Contract Auditor",
        "Audits Solidity/EVM contracts for exploitable vulnerabilities.",
        """
You are the Smart Contract Auditor. You review contracts for reentrancy,
access control failures, arithmetic and rounding errors, oracle and price
manipulation, flash-loan and MEV exposure, upgrade and initialisation
mistakes, signature and replay issues, and economic invariants that can be
broken. For every candidate issue you give the exact function and line, the
precondition, the concrete attack sequence and the impact in funds at risk.
You distinguish a real vulnerability from a code smell, and you say which
ones you actually verified.
""",
        temperature=0.2,
        max_iterations=40,
        tools=(*READONLY_CODING_TOOL_SLUGS, "echo", "now", "calculate", "http_get"),
        permissions=("filesystem:read", "tool:read"),
    ),
    _definition(
        "web-security",
        "Web Security Researcher",
        "Tests web applications for authentication, session and injection flaws.",
        """
You are the Web Security Researcher. You reason about authentication and
session handling, authorisation and object-level access, input handling and
injection sinks, server-side request forgery, file handling, caching and
race conditions, and client-side trust mistakes. You produce reproducible
steps with the smallest harmless payload that demonstrates the issue, and you
never run an intrusive test against a system the task did not name.
""",
        temperature=0.2,
        max_iterations=40,
        tools=("http_get", "echo", "now", "calculate"),
        permissions=("tool:read",),
    ),
    _definition(
        "api-security",
        "API Security Researcher",
        "Reviews REST, GraphQL and RPC interfaces for authorisation and logic flaws.",
        """
You are the API Security Researcher. You review API surfaces for broken
object and function-level authorisation, mass assignment, excessive data
exposure, rate-limit and quota gaps, unsafe deserialisation, GraphQL
introspection and batching abuse, inconsistent tenant isolation and business
logic that can be driven out of order. You map each finding to the endpoint,
the required role and the exact request that proves it.
""",
        temperature=0.2,
        max_iterations=40,
        tools=("http_get", "echo", "now", "calculate"),
        permissions=("tool:read",),
    ),
    _definition(
        "vuln-validation",
        "Vulnerability Validation Agent",
        "Independently reproduces candidate findings and rejects false positives.",
        """
You are the Vulnerability Validation Agent. You take candidate findings and
try to break them. For each one you either reproduce the impact with a minimal
sequence of steps, or you reject it with the reason the original reasoning
failed: wrong precondition, unreachable code, mitigated by a control, or
impact that does not cross a security boundary. Your default is rejection
until reproduction succeeds. You never upgrade severity to make a finding look
better, and you never accept "could be exploitable" as confirmed.
""",
        temperature=0.1,
        max_iterations=40,
        tools=("echo", "now", "calculate", "http_get"),
        permissions=("tool:read",),
    ),
    _definition(
        "testing",
        "Testing Agent",
        "Writes and runs tests that pin down the behaviour a change relies on.",
        """
You are the Testing Agent. You write focused tests that fail for the right
reason before the fix and pass after it, and you report the exact command you
ran with its real output. You prefer a small number of meaningful assertions
over broad shallow coverage. You never report a test as passing unless you
executed it, and you flag flaky or environment-dependent tests explicitly.
""",
        temperature=0.1,
        max_iterations=40,
        tools=(*READONLY_CODING_TOOL_SLUGS, "run_command", "http_get", "echo", "now", "calculate"),
        permissions=("filesystem:read", "tools:execute", "tool:read"),
    ),
    _definition(
        "optimization",
        "Optimization Agent",
        "Improves performance and cost with measurements before and after.",
        """
You are the Optimization Agent. You profile before you change anything, and
you report a measured before/after number for every optimisation. You prefer
algorithmic and allocation fixes to micro-tuning, you state the trade-offs you
accepted, and you refuse to optimise without a baseline. If a measurement is
not possible in this environment you say so instead of estimating.
""",
        temperature=0.1,
        max_iterations=30,
        tools=(*READONLY_CODING_TOOL_SLUGS, "run_command", "http_get", "echo", "now", "calculate"),
        permissions=("filesystem:read", "tools:execute", "tool:read"),
    ),
    _definition(
        "report-writer",
        "Report Writer Agent",
        "Turns verified findings into submission-ready reports.",
        """
You are the Report Writer Agent. You assemble the outputs of earlier steps
into a report with: summary, scope, severity and impact per finding,
reproduction steps, evidence, remediation, and references. You write for the
program's triage engineer: no marketing language, no inflated severity, no
claims that the evidence does not support. Every finding you include must
carry its reproduction and its evidence; anything unverified goes into an
"unverified leads" section instead of the findings.
""",
        temperature=0.3,
        max_iterations=20,
        tools=(*READONLY_CODING_TOOL_SLUGS, "http_get", "echo", "now", "calculate"),
        permissions=("filesystem:read", "tool:read"),
    ),
    _definition(
        "independent-reviewer",
        "Independent Reviewer Agent",
        "Challenges the team's conclusions as an adversarial reviewer.",
        """
You are the Independent Reviewer Agent. Your job is to find what the other
agents got wrong: unstated assumptions, missing preconditions, severity that
does not match impact, reproduction steps that do not actually reproduce, and
conclusions that exceed the evidence. You do not rewrite the report; you list
objections ranked by how much they would damage the submission if a triager
raised them, each with the specific change that would resolve it.
""",
        temperature=0.2,
        max_iterations=25,
        tools=(*READONLY_CODING_TOOL_SLUGS, "echo", "now", "calculate", "http_get"),
        permissions=("filesystem:read", "tool:read"),
    ),
    _definition(
        "compliance",
        "Compliance Agent",
        "Checks a planned action against programme rules, policy and law.",
        """
You are the Compliance Agent. You check proposed actions against the target
program's scope and rules of engagement, the platform's own policies, and
applicable disclosure and data-handling obligations. You output a clear
verdict per action: allowed, allowed with conditions, or prohibited, with the
rule you are relying on. When the rules are ambiguous you say so and require a
human decision rather than guessing.
""",
        temperature=0.1,
        max_iterations=20,
        tools=("echo", "now", "calculate", "http_get"),
        permissions=("tool:read",),
    ),
)

SPECIALISTS_BY_SLUG: dict[str, SpecialistDefinition] = {
    definition.slug: definition for definition in SPECIALISTS
}


def load_overrides(path: str | Path | None) -> dict[str, dict[str, Any]]:
    """Read a YAML/JSON overrides file keyed by agent slug."""
    if not path:
        return {}
    file = Path(path)
    if not file.exists():
        raise UnprocessableError(f"Specialist override file not found: {file}")
    raw = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    if isinstance(raw, dict) and "agents" in raw:
        raw = raw["agents"]
    if not isinstance(raw, dict):
        raise UnprocessableError("The specialist override file must map slug -> settings")
    result: dict[str, dict[str, Any]] = {}
    for slug, settings in raw.items():
        if not isinstance(settings, dict):
            raise UnprocessableError(f"Overrides for '{slug}' must be a mapping")
        result[str(slug)] = settings
    return result


def merged_definitions(
    overrides: dict[str, dict[str, Any]] | None = None,
) -> list[SpecialistDefinition]:
    """Apply overrides over the built-in definitions."""
    overrides = overrides or {}
    unknown = set(overrides) - set(SPECIALISTS_BY_SLUG)
    if unknown:
        raise UnprocessableError(
            "Override file references unknown specialist agents",
            details={"slugs": sorted(unknown)},
        )

    merged: list[SpecialistDefinition] = []
    for definition in SPECIALISTS:
        settings = overrides.get(definition.slug, {})
        payload = definition.to_dict()
        payload.update(settings)
        tools = payload.get("tools") or []
        permissions = payload.get("permissions") or []
        if isinstance(tools, str):
            tools = [tools]
        if isinstance(permissions, str):
            permissions = [permissions]
        # Memory and skill access is part of being a specialist, so it is merged in
        # after overrides rather than being something an override can switch off.
        tools = list(dict.fromkeys((*tools, *MEMORY_TOOL_SLUGS)))
        permissions = list(dict.fromkeys((*permissions, "tool:read", MEMORY_WRITE_PERMISSION)))
        merged.append(
            SpecialistDefinition(
                slug=payload["slug"],
                name=payload["name"],
                description=payload["description"],
                system_prompt=payload["system_prompt"],
                temperature=float(payload.get("temperature", 0.2)),
                max_iterations=int(payload.get("max_iterations", 25)),
                token_budget=int(payload.get("token_budget", 200_000)),
                timeout_seconds=int(payload.get("timeout_seconds", 1800)),
                tools=tuple(str(item) for item in tools),
                permissions=tuple(str(item) for item in permissions),
                is_orchestrator=bool(payload.get("is_orchestrator", definition.is_orchestrator)),
                metadata=dict(payload.get("metadata") or {}),
            )
        )
    return merged


@dataclass(slots=True)
class SyncReport:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    skipped_tools: dict[str, list[str]] = field(default_factory=dict)
    granted_permissions: dict[str, list[str]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "created": self.created,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "skipped_tools": self.skipped_tools,
            "granted_permissions": self.granted_permissions,
        }


async def sync_specialists(
    db: AsyncSession,
    *,
    overrides: dict[str, dict[str, Any]] | None = None,
    default_provider_id: UUID | None = None,
    default_model: str | None = None,
    overwrite: bool = False,
    created_by: UUID | None = None,
) -> SyncReport:
    """Create missing specialist agents and (optionally) refresh existing ones.

    Existing agents are only modified when ``overwrite`` is set, so operator
    edits survive a restart. Tools are assigned when they exist in the
    registry; unknown assignments are reported instead of failing the seed.
    """
    from app.db.models import AgentTool  # local import keeps the module light

    report = SyncReport()
    known_tools = {
        str(slug)
        for slug in (
            await db.execute(select(ToolRegistry.slug).where(ToolRegistry.enabled.is_(True)))
        ).scalars()
    }

    for definition in merged_definitions(overrides):
        agent = (
            await db.execute(select(Agent).where(Agent.slug == definition.slug))
        ).scalar_one_or_none()
        if agent is None:
            agent = Agent(
                slug=definition.slug,
                name=definition.name,
                description=definition.description,
                system_prompt=definition.system_prompt,
                model_provider_id=default_provider_id,
                model_name=default_model,
                temperature=definition.temperature,
                max_iterations=definition.max_iterations,
                token_budget=definition.token_budget,
                timeout_seconds=definition.timeout_seconds,
                permissions={"tools": list(definition.permissions)},
                memory_config={},
                status=AgentStatus.ACTIVE,
                is_system=True,
                created_by=created_by,
            )
            db.add(agent)
            await db.flush()
            report.created.append(definition.slug)
        elif overwrite:
            agent.name = definition.name
            agent.description = definition.description
            agent.system_prompt = definition.system_prompt
            if default_provider_id is not None:
                agent.model_provider_id = default_provider_id
            if default_model is not None:
                agent.model_name = default_model
            agent.temperature = definition.temperature
            agent.max_iterations = definition.max_iterations
            agent.token_budget = definition.token_budget
            agent.timeout_seconds = definition.timeout_seconds
            agent.permissions = {"tools": list(definition.permissions)}
            agent.is_system = True
            agent.version += 1
            await db.flush()
            report.updated.append(definition.slug)
        else:
            report.unchanged.append(definition.slug)

        # Grant newly required permissions additively. An existing agent keeps its
        # own tool list; only what the definition needs is unioned in, so an
        # operator's extra grants survive a restart.
        config = dict(agent.permissions or {})
        current_permissions = [
            str(item) for item in (config.get("tools") or []) if str(item).strip()
        ]
        added = [
            permission
            for permission in definition.permissions
            if permission not in current_permissions
        ]
        if added:
            config["tools"] = [*current_permissions, *added]
            agent.permissions = config
            report.granted_permissions[definition.slug] = added

        missing = [slug for slug in definition.tools if slug not in known_tools]
        if missing:
            report.skipped_tools[definition.slug] = missing
        assignable = [slug for slug in definition.tools if slug in known_tools]
        if not assignable:
            continue

        existing = {
            str(row)
            for row in (
                await db.execute(
                    select(ToolRegistry.slug)
                    .join(AgentTool, AgentTool.tool_id == ToolRegistry.id)
                    .where(AgentTool.agent_id == agent.id)
                )
            ).scalars()
        }
        for slug in assignable:
            if slug in existing:
                continue
            tool_id = (
                await db.execute(select(ToolRegistry.id).where(ToolRegistry.slug == slug))
            ).scalar_one()
            db.add(AgentTool(agent_id=agent.id, tool_id=tool_id, enabled=True, config={}))

    await db.flush()
    logger.info(
        "specialists_synced",
        created=len(report.created),
        updated=len(report.updated),
        unchanged=len(report.unchanged),
    )
    return report


async def specialist_catalogue(db: AsyncSession) -> dict[str, str]:
    """Slug -> description for agents that are active and usable as delegates."""
    result = await db.execute(
        select(Agent).where(Agent.status == AgentStatus.ACTIVE).order_by(Agent.slug)
    )
    return {agent.slug: agent.description or agent.name for agent in result.scalars()}
