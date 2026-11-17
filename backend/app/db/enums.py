"""Domain enumerations shared by the database models and API schemas."""

from __future__ import annotations

from enum import StrEnum


class UserRole(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    OPERATOR = "operator"
    VIEWER = "viewer"


class AgentStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"


class TaskStatus(StrEnum):
    PENDING = "pending"
    PLANNING = "planning"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_TASK_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)


class StepKind(StrEnum):
    PLAN = "plan"
    LLM = "llm"
    TOOL = "tool"
    AGENT = "agent"
    APPROVAL = "approval"
    CHECKPOINT = "checkpoint"
    VERIFICATION = "verification"


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class EventLevel(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class MemoryLayer(StrEnum):
    WORKING = "working"
    CONVERSATION = "conversation"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROJECT = "project"
    AGENT = "agent"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ExecutionEnvironment(StrEnum):
    HOST = "host"
    SANDBOX = "sandbox"
    BROWSER = "browser"
    REMOTE = "remote"


class ProjectStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class CodingSessionStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


class CompetitionEntryStatus(StrEnum):
    """Lifecycle of our entry into an external competition.

    ``DRAFT`` and ``PREPARED`` are local-only: nothing has left the platform.
    ``AWAITING_APPROVAL`` means a human must sign off before submission, and
    ``SUBMITTED`` onwards mirrors what the platform reports.
    """

    DRAFT = "draft"
    PREPARED = "prepared"
    AWAITING_APPROVAL = "awaiting_approval"
    SUBMITTED = "submitted"
    REJECTED = "rejected"
    VALIDATING = "validating"
    GRADED = "graded"
    PLACED = "placed"
    WON = "won"
    FAILED = "failed"
    WITHDRAWN = "withdrawn"


class RepositoryProvider(StrEnum):
    GITHUB = "github"
    GITLAB = "gitlab"
    LOCAL = "local"
    OTHER = "other"


class BountyPlatform(StrEnum):
    MAKE_FUN = "make_fun"
    BUGCROWD = "bugcrowd"
    HACKERONE = "hackerone"
    IMMUNEFI = "immunefi"
    OTHER = "other"


class CompetitionStatus(StrEnum):
    DISCOVERED = "discovered"
    EVALUATING = "evaluating"
    IN_PROGRESS = "in_progress"
    SUBMITTED = "submitted"
    GRADED = "graded"
    CLOSED = "closed"
    ARCHIVED = "archived"


class FindingCategory(StrEnum):
    WEB = "web"
    API = "api"
    GRAPHQL = "graphql"
    SMART_CONTRACT = "smart_contract"
    CRYPTOGRAPHY = "cryptography"
    BUSINESS_LOGIC = "business_logic"
    DEPENDENCY = "dependency"
    OTHER = "other"


class Severity(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class FindingStatus(StrEnum):
    HYPOTHESIS = "hypothesis"
    VALIDATING = "validating"
    CONFIRMED = "confirmed"
    FALSE_POSITIVE = "false_positive"
    REPORTED = "reported"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class ApprovalAction(StrEnum):
    SUBMIT_COMPETITION = "submit_competition"
    RUN_HIGH_RISK_TOOL = "run_high_risk_tool"
    EXTERNAL_REPORT = "external_report"
    DEPLOY = "deploy"
    SPEND_BUDGET = "spend_budget"
    OTHER = "other"


class ProviderType(StrEnum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GOOGLE = "google"
    OPENROUTER = "openrouter"
    LOCAL = "local"
    OPENAI_COMPATIBLE = "openai_compatible"
    CODEX_RESPONSES = "codex_responses"


class ModelUsageStatus(StrEnum):
    SUCCESS = "success"
    ERROR = "error"


class ConversationChannel(StrEnum):
    WEB = "web"
    TELEGRAM = "telegram"
    API = "api"
    SYSTEM = "system"


class ConversationStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


class TaskDependencyType(StrEnum):
    FINISH_TO_START = "finish_to_start"
    START_TO_START = "start_to_start"
