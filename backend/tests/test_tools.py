"""Tests for the tool interface, built-ins and enforcement."""

from __future__ import annotations

import pytest

from app.db.enums import RiskLevel
from app.tools.base import Tool, ToolContext, ToolRegistry
from app.tools.builtin import (
    BUILTIN_TOOLS,
    CalculatorTool,
    EchoTool,
    default_tool_registry,
    evaluate_expression,
    validate_public_url,
)
from app.tools.errors import (
    ApprovalRequiredError,
    ToolExecutionError,
    ToolNotFoundError,
    ToolPermissionError,
    ToolValidationError,
)


class DangerousTool(Tool):
    slug = "danger"
    name = "Danger"
    description = "High risk test tool"
    parameters = {"type": "object", "properties": {}, "additionalProperties": False}
    risk_level = RiskLevel.HIGH
    timeout_seconds = 2

    async def execute(self, arguments, context):
        return {"ran": True}


class SlowTool(Tool):
    slug = "slow"
    name = "Slow"
    description = "Sleeps past its timeout"
    timeout_seconds = 1

    async def execute(self, arguments, context):
        import asyncio

        await asyncio.sleep(5)
        return {}


class SecureTool(Tool):
    slug = "secure"
    name = "Secure"
    description = "Requires a permission"
    required_permissions = ("tool:admin",)

    async def execute(self, arguments, context):
        return {"ok": True}


def test_builtin_registry_contents() -> None:
    registry = default_tool_registry()
    assert {tool.slug for tool in registry.all()} == {
        "calculate",
        "delete_file",
        "echo",
        "git",
        "http_get",
        "list_files",
        "load_skill",
        "makefun_competition_details",
        "makefun_competitions",
        "makefun_prepare_entry",
        "makefun_submit_entry",
        "now",
        "read_file",
        "recall",
        "remember",
        "research_scopes",
        "research_hypothesis",
        "research_report",
        "run_command",
        "search_code",
        "write_file",
    }
    assert len(BUILTIN_TOOLS) == 21


async def test_echo_tool() -> None:
    result = await EchoTool().run({"text": "hello"}, ToolContext())
    assert result.ok
    assert result.output["text"] == "hello"


async def test_argument_validation_rejects_missing_field() -> None:
    with pytest.raises(ToolValidationError):
        await EchoTool().run({}, ToolContext())


async def test_argument_validation_rejects_extra_fields() -> None:
    with pytest.raises(ToolValidationError):
        await EchoTool().run({"text": "a", "extra": 1}, ToolContext())


async def test_unknown_tool_raises() -> None:
    with pytest.raises(ToolNotFoundError):
        ToolRegistry().get("nope")


def test_calculator_arithmetic() -> None:
    assert evaluate_expression("2 + 3 * 4") == 14
    assert evaluate_expression("(2 + 3) * 4") == 20
    assert evaluate_expression("2 ** 10") == 1024
    assert evaluate_expression("sqrt(16) + abs(-2)") == 6
    assert isinstance(evaluate_expression("7 / 2"), float)


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('id')",
        "open('/etc/passwd')",
        "[1, 2, 3]",
        "'a' * 3",
        "2 ** 100000",
        "1 / 0",
        "unknown_name",
        "lambda: 1",
        "1 if 1 else 0",
    ],
)
def test_calculator_blocks_unsafe_expressions(expression: str) -> None:
    with pytest.raises(ToolExecutionError):
        evaluate_expression(expression)


def test_calculator_rejects_long_expression() -> None:
    with pytest.raises(ToolExecutionError):
        evaluate_expression("1+" * 300 + "1")


async def test_calculator_tool_runs() -> None:
    result = await CalculatorTool().run({"expression": "3 * (4 + 5)"}, ToolContext())
    assert result.ok
    assert result.output["result"] == 27


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/x",
        "http://127.0.0.1/admin",
        "http://localhost:8000",
        "http://169.254.169.254/latest/meta-data",
        "http://10.0.0.5/",
        "http://[::1]/",
    ],
)
def test_ssrf_guard_blocks_internal_targets(url: str) -> None:
    with pytest.raises(ToolExecutionError):
        validate_public_url(url)


def test_ssrf_guard_allows_public_host() -> None:
    assert validate_public_url("https://example.com/path") == "https://example.com/path"


async def test_high_risk_tool_requires_approval() -> None:
    tool = DangerousTool()
    assert tool.requires_approval
    with pytest.raises(ApprovalRequiredError):
        await tool.run({}, ToolContext())
    result = await tool.run({}, ToolContext(approvals=frozenset({"danger"})))
    assert result.ok


async def test_permission_requirement_enforced() -> None:
    with pytest.raises(ToolPermissionError):
        await SecureTool().run({}, ToolContext(permissions=frozenset({"tool:read"})))
    result = await SecureTool().run({}, ToolContext(permissions=frozenset({"tool:admin"})))
    assert result.ok


async def test_wildcard_permission_grants_everything() -> None:
    result = await SecureTool().run({}, ToolContext(permissions=frozenset({"*"})))
    assert result.ok


async def test_timeout_produces_failed_result() -> None:
    result = await SlowTool().run({}, ToolContext())
    assert result.ok is False
    assert "timed out" in (result.error or "")


def test_registry_filters_unavailable_tools() -> None:
    registry = ToolRegistry([EchoTool(), SecureTool(), DangerousTool()])
    context = ToolContext(permissions=frozenset({"tool:read"}))
    assert [tool.slug for tool in registry.allowed_for(context)] == ["echo"]


def test_registry_rejects_duplicates() -> None:
    registry = ToolRegistry([EchoTool()])
    with pytest.raises(ValueError):
        registry.register(EchoTool())


def test_tool_spec_declaration() -> None:
    spec = CalculatorTool().spec_dict()
    assert spec["slug"] == "calculate"
    assert spec["risk_level"] == "low"
    assert spec["requires_approval"] is False
    assert spec["parameters"]["required"] == ["expression"]
