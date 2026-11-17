"""Runtime errors."""

from __future__ import annotations

from app.core.exceptions import AppError


class RuntimeError_(AppError):
    status_code = 500
    code = "runtime_error"
    message = "The agent runtime failed"


class BudgetExceededError(AppError):
    status_code = 402
    code = "budget_exceeded"
    message = "The agent exceeded its token or cost budget"


class IterationLimitError(AppError):
    status_code = 409
    code = "iteration_limit"
    message = "The agent reached its maximum iteration count"


class TaskTimeoutError(AppError):
    status_code = 504
    code = "task_timeout"
    message = "The agent exceeded its execution timeout"


class TaskCancelledError(AppError):
    status_code = 409
    code = "task_cancelled"
    message = "The task was cancelled"
