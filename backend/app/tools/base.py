"""Standardised tool interface.

Every tool declares its input schema, required permissions, risk level,
timeout and execution environment. The runtime enforces those declarations;
tools never decide on their own whether they may run.
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, ClassVar
from uuid import UUID

import jsonschema
import structlog

from app.db.enums import ExecutionEnvironment, RiskLevel
from app.tools.errors import (
    ApprovalRequiredError,
    ToolExecutionError,
    ToolNotFoundError,
    ToolPermissionError,
    ToolValidationError,
)

logger = structlog.get_logger("agentos.tools")

_RISK_ORDER = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
    RiskLevel.CRITICAL: 3,
}


@dataclass(slots=True)
class ToolContext:
    """Everything a tool is allowed to know about its caller."""

    agent_id: UUID | None = None
    task_id: UUID | None = None
    step_id: UUID | None = None
    workspace_root: str | None = None
    permissions: frozenset[str] = frozenset()
    approvals: frozenset[str] = frozenset()
    metadata: dict[str, Any] = field(default_factory=dict)
    # Capabilities injected by the execution layer (skill lookup, memory store).
    # Tools stay database-agnostic: they receive callables, never a session.
    resources: dict[str, Any] = field(default_factory=dict)

    def has_permission(self, permission: str) -> bool:
        return "*" in self.permissions or permission in self.permissions

    def is_approved(self, tool_slug: str) -> bool:
        return tool_slug in self.approvals or "*" in self.approvals


@dataclass(slots=True)
class ToolResult:
    ok: bool
    output: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    duration_ms: int = 0

    def as_text(self) -> str:
        """Render the result for a model tool-result message."""
        if not self.ok:
            return f"ERROR: {self.error or 'tool failed'}"
        import json

        return json.dumps(self.output, ensure_ascii=False, default=str)


class Tool(ABC):
    """Base class for built-in and plugin tools."""

    slug: ClassVar[str]
    name: ClassVar[str]
    description: ClassVar[str]
    parameters: ClassVar[dict[str, Any]] = {"type": "object", "properties": {}}
    required_permissions: ClassVar[tuple[str, ...]] = ()
    risk_level: ClassVar[RiskLevel] = RiskLevel.LOW
    execution_environment: ClassVar[ExecutionEnvironment] = ExecutionEnvironment.SANDBOX
    timeout_seconds: ClassVar[int] = 30
    enabled: ClassVar[bool] = True

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        for attribute in ("slug", "name", "description"):
            if not getattr(cls, attribute, None):
                raise TypeError(f"{cls.__name__} must define '{attribute}'")

    @property
    def requires_approval(self) -> bool:
        return _RISK_ORDER[self.risk_level] >= _RISK_ORDER[RiskLevel.HIGH]

    def validate_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            jsonschema.validate(instance=arguments, schema=self.parameters)
        except jsonschema.ValidationError as exc:
            location = "/".join(str(part) for part in exc.absolute_path) or "<root>"
            raise ToolValidationError(
                f"Invalid arguments for tool '{self.slug}' at {location}: {exc.message}"
            ) from exc
        return arguments

    def check_permissions(self, context: ToolContext) -> None:
        missing = [p for p in self.required_permissions if not context.has_permission(p)]
        if missing:
            raise ToolPermissionError(
                f"Tool '{self.slug}' requires permissions: {', '.join(sorted(missing))}"
            )
        if self.requires_approval and not context.is_approved(self.slug):
            raise ApprovalRequiredError(
                f"Tool '{self.slug}' has risk level '{self.risk_level.value}' and needs approval"
            )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        """Validate, authorise and execute the tool with a hard timeout."""
        payload = self.validate_arguments(arguments)
        self.check_permissions(context)
        started = time.perf_counter()
        try:
            output = await asyncio.wait_for(
                self.execute(payload, context), timeout=self.timeout_seconds
            )
        except TimeoutError:
            duration = int((time.perf_counter() - started) * 1000)
            logger.warning("tool_timeout", tool=self.slug, timeout=self.timeout_seconds)
            return ToolResult(
                ok=False,
                error=f"Tool timed out after {self.timeout_seconds}s",
                duration_ms=duration,
            )
        except ToolExecutionError as exc:
            duration = int((time.perf_counter() - started) * 1000)
            return ToolResult(ok=False, error=str(exc), duration_ms=duration)
        duration = int((time.perf_counter() - started) * 1000)
        return ToolResult(ok=True, output=output, duration_ms=duration)

    @abstractmethod
    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        """Perform the work. Raise :class:`ToolExecutionError` on failure."""

    def spec_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "required_permissions": list(self.required_permissions),
            "risk_level": self.risk_level.value,
            "execution_environment": self.execution_environment.value,
            "timeout_seconds": self.timeout_seconds,
            "enabled": self.enabled,
            "requires_approval": self.requires_approval,
        }


class ToolRegistry:
    """In-process registry of available tools."""

    def __init__(self, tools: Iterable[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools or ():
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if tool.slug in self._tools:
            raise ValueError(f"Tool '{tool.slug}' is already registered")
        self._tools[tool.slug] = tool

    def get(self, slug: str) -> Tool:
        tool = self._tools.get(slug)
        if tool is None:
            raise ToolNotFoundError(f"Tool '{slug}' is not available")
        return tool

    def all(self) -> list[Tool]:
        return [tool for tool in self._tools.values() if tool.enabled]

    def allowed_for(
        self, context: ToolContext, *, include_approval_gated: bool = False
    ) -> list[Tool]:
        """Tools the caller may use, sorted by slug.

        ``include_approval_gated`` advertises tools whose permissions are held
        but which still need human sign-off: the model may then ask for them,
        and the runtime turns the request into an approval instead of running
        it. Permission failures are never included.
        """
        allowed: list[Tool] = []
        for tool in self.all():
            if any(not context.has_permission(p) for p in tool.required_permissions):
                continue
            if (
                not include_approval_gated
                and tool.requires_approval
                and not context.is_approved(tool.slug)
            ):
                continue
            allowed.append(tool)
        return sorted(allowed, key=lambda tool: tool.slug)

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, slug: object) -> bool:
        return slug in self._tools
