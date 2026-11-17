"""Tool system: declarations, registry, permission checks and built-ins."""

from app.tools.base import Tool, ToolContext, ToolRegistry, ToolResult
from app.tools.builtin import BUILTIN_TOOLS, default_tool_registry
from app.tools.coding import CODING_TOOLS
from app.tools.errors import (
    ApprovalRequiredError,
    ToolError,
    ToolExecutionError,
    ToolNotFoundError,
    ToolPermissionError,
    ToolValidationError,
)
from app.tools.makefun import MAKEFUN_TOOLS
from app.tools.memory import LoadSkillTool, MemorySearchTool, MemoryWriteTool

__all__ = [
    "BUILTIN_TOOLS",
    "CODING_TOOLS",
    "MAKEFUN_TOOLS",
    "ApprovalRequiredError",
    "LoadSkillTool",
    "MemorySearchTool",
    "MemoryWriteTool",
    "Tool",
    "ToolContext",
    "ToolError",
    "ToolExecutionError",
    "ToolNotFoundError",
    "ToolPermissionError",
    "ToolRegistry",
    "ToolResult",
    "ToolValidationError",
    "default_tool_registry",
]
