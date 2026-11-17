"""Tool errors mapped onto the platform's HTTP error hierarchy."""

from __future__ import annotations

from app.core.exceptions import AppError


class ToolError(AppError):
    status_code = 400
    code = "tool_error"
    message = "The tool could not be used"


class ToolNotFoundError(ToolError):
    status_code = 404
    code = "tool_not_found"
    message = "The requested tool does not exist"


class ToolValidationError(ToolError):
    code = "tool_invalid_arguments"
    message = "The tool arguments are invalid"


class ToolPermissionError(ToolError):
    status_code = 403
    code = "tool_permission_denied"
    message = "The tool permission requirements are not met"


class ApprovalRequiredError(AppError):
    status_code = 409
    code = "approval_required"
    message = "This action requires human approval"


class ToolExecutionError(ToolError):
    status_code = 502
    code = "tool_execution_failed"
    message = "The tool failed while executing"
