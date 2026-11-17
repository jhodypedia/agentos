"""Built-in tools that are safe to expose to a model by default.

Everything here is read-only or purely computational, plus the skill/memory
tools which only read and write this platform's own records. The workspace
tools (``app/tools/coding.py``) live behind the sandbox and approval gates: the
read-only ones are ``low`` risk, everything that writes or executes is ``high``.
"""

from __future__ import annotations

import ast
import asyncio
import ipaddress
import math
import operator
import socket
from datetime import UTC, datetime
from typing import Any, ClassVar
from urllib.parse import urlparse

import httpx

from app.db.enums import ExecutionEnvironment, RiskLevel
from app.tools.base import Tool, ToolContext
from app.tools.coding import CODING_TOOLS
from app.tools.errors import ToolExecutionError
from app.tools.makefun import MAKEFUN_TOOLS
from app.tools.memory import LoadSkillTool, MemorySearchTool, MemoryWriteTool
from app.tools.research import RESEARCH_TOOLS

_MAX_HTTP_BYTES = 100_000
_MAX_HTTP_CHARS = 20_000


class EchoTool(Tool):
    slug = "echo"
    name = "Echo"
    description = "Return the provided text unchanged. Useful for connectivity checks."
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"text": {"type": "string", "description": "Text to echo back"}},
        "required": ["text"],
        "additionalProperties": False,
    }
    risk_level = RiskLevel.LOW
    execution_environment = ExecutionEnvironment.HOST
    timeout_seconds = 5

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        return {"text": str(arguments["text"])}


class NowTool(Tool):
    slug = "now"
    name = "Current time"
    description = "Return the current UTC date and time in ISO 8601 format."
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    risk_level = RiskLevel.LOW
    execution_environment = ExecutionEnvironment.HOST
    timeout_seconds = 5

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        now = datetime.now(UTC)
        return {"utc": now.isoformat(), "unix": int(now.timestamp())}


_BIN_OPS: dict[type[ast.operator], Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_UNARY_OPS: dict[type[ast.unaryop], Any] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

_FUNCTIONS: dict[str, Any] = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sum": sum,
    "sqrt": math.sqrt,
    "floor": math.floor,
    "ceil": math.ceil,
    "log": math.log,
    "exp": math.exp,
}

_CONSTANTS: dict[str, float] = {"pi": math.pi, "e": math.e}
_MAX_POWER = 1000


def evaluate_expression(expression: str) -> float | int:
    """Evaluate a numeric expression using a restricted AST visitor."""
    if len(expression) > 500:
        raise ToolExecutionError("Expression is too long")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ToolExecutionError("Expression is not valid syntax") from exc
    return _eval_node(tree.body, depth=0)


def _eval_node(node: ast.AST, *, depth: int) -> float | int:
    if depth > 32:
        raise ToolExecutionError("Expression is nested too deeply")

    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, int | float):
            raise ToolExecutionError("Only numeric literals are allowed")
        return float(node.value) if isinstance(node.value, float) else int(node.value)

    if isinstance(node, ast.Name):
        if node.id in _CONSTANTS:
            return _CONSTANTS[node.id]
        raise ToolExecutionError(f"Unknown name '{node.id}'")

    if isinstance(node, ast.BinOp):
        operation = _BIN_OPS.get(type(node.op))
        if operation is None:
            raise ToolExecutionError("Unsupported operator")
        left = _eval_node(node.left, depth=depth + 1)
        right = _eval_node(node.right, depth=depth + 1)
        if isinstance(node.op, ast.Pow) and abs(right) > _MAX_POWER:
            raise ToolExecutionError("Exponent is too large")
        try:
            result = operation(left, right)
        except ZeroDivisionError as exc:
            raise ToolExecutionError("Division by zero") from exc
        except OverflowError as exc:
            raise ToolExecutionError("Numeric overflow") from exc
        if isinstance(result, complex):
            raise ToolExecutionError("Complex results are not supported")
        return float(result) if isinstance(result, float) else int(result)

    if isinstance(node, ast.UnaryOp):
        operation = _UNARY_OPS.get(type(node.op))
        if operation is None:
            raise ToolExecutionError("Unsupported unary operator")
        operand = _eval_node(node.operand, depth=depth + 1)
        result = operation(operand)
        return float(result) if isinstance(result, float) else int(result)

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCTIONS:
            raise ToolExecutionError("Unsupported function")
        if node.keywords:
            raise ToolExecutionError("Keyword arguments are not supported")
        args = [_eval_node(arg, depth=depth + 1) for arg in node.args]
        try:
            result = _FUNCTIONS[node.func.id](*args)
        except (TypeError, ValueError) as exc:
            raise ToolExecutionError(f"Invalid arguments for {node.func.id}") from exc
        if isinstance(result, complex):
            raise ToolExecutionError("Complex results are not supported")
        return float(result) if isinstance(result, float) else int(result)

    raise ToolExecutionError(f"Unsupported expression element: {type(node).__name__}")


class CalculatorTool(Tool):
    slug = "calculate"
    name = "Calculator"
    description = (
        "Evaluate a numeric arithmetic expression. Supports + - * / // % **, "
        "parentheses, and abs, round, min, max, sum, sqrt, floor, ceil, log, exp, pi, e."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "Arithmetic expression, e.g. '(3 + 4) * 2 ** 3'",
                "maxLength": 500,
            }
        },
        "required": ["expression"],
        "additionalProperties": False,
    }
    risk_level = RiskLevel.LOW
    execution_environment = ExecutionEnvironment.HOST
    timeout_seconds = 5

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        expression = str(arguments["expression"])
        value = evaluate_expression(expression)
        return {"expression": expression, "result": value}


def _resolve_public_addresses(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(str(host), int(port), proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ToolExecutionError(f"Could not resolve host '{host}'") from exc

    addresses: list[str] = []
    for info in infos:
        address = str(info[4][0])
        ip = ipaddress.ip_address(address)
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise ToolExecutionError(
                "Requests to private, loopback or reserved addresses are not allowed"
            )
        addresses.append(address)
    if not addresses:
        raise ToolExecutionError(f"Could not resolve host '{host}'")
    return addresses


def validate_public_url(url: str) -> str:
    """Reject non-HTTP schemes and hosts that resolve to internal addresses."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ToolExecutionError("Only http and https URLs are allowed")
    if not parsed.hostname:
        raise ToolExecutionError("The URL must include a host")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    _resolve_public_addresses(parsed.hostname, port)
    return url


class HttpGetTool(Tool):
    slug = "http_get"
    name = "HTTP GET"
    description = (
        "Fetch a public HTTP(S) URL and return the status, content type and a "
        "truncated body. Private and loopback addresses are blocked."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Absolute http(s) URL to fetch"},
            "max_chars": {
                "type": "integer",
                "minimum": 100,
                "maximum": _MAX_HTTP_CHARS,
                "default": 4000,
            },
        },
        "required": ["url"],
        "additionalProperties": False,
    }
    risk_level = RiskLevel.MEDIUM
    execution_environment = ExecutionEnvironment.SANDBOX
    timeout_seconds = 20

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        url = str(arguments["url"])
        max_chars = int(arguments.get("max_chars", 4000))
        await asyncio.to_thread(validate_public_url, url)

        try:
            async with httpx.AsyncClient(
                timeout=float(self.timeout_seconds - 1),
                follow_redirects=False,
                headers={"User-Agent": "AgentOS/0.1 (+tool:http_get)"},
            ) as client:
                response = await client.get(url)
                body = response.text[:_MAX_HTTP_BYTES]
        except httpx.TimeoutException as exc:
            raise ToolExecutionError("The request timed out") from exc
        except httpx.HTTPError as exc:
            raise ToolExecutionError(f"The request failed: {exc}") from exc

        truncated = len(body) > max_chars
        return {
            "url": url,
            "status_code": response.status_code,
            "content_type": response.headers.get("content-type", ""),
            "body": body[:max_chars],
            "truncated": truncated,
        }


BUILTIN_TOOLS: tuple[type[Tool], ...] = (
    EchoTool,
    NowTool,
    CalculatorTool,
    HttpGetTool,
    LoadSkillTool,
    MemorySearchTool,
    MemoryWriteTool,
    *CODING_TOOLS,
    *MAKEFUN_TOOLS,
    *RESEARCH_TOOLS,
)


def default_tool_registry() -> Any:
    """Return a registry populated with every built-in tool."""
    from app.tools.base import ToolRegistry

    return ToolRegistry(tool() for tool in BUILTIN_TOOLS)
