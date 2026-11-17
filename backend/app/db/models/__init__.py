"""Import every model so SQLAlchemy's registry and Alembic see all tables."""

from app.db.models.agent import Agent, AgentConfig, AgentSkill, AgentTool
from app.db.models.approval import Approval
from app.db.models.audit import AuditLog
from app.db.models.bounty import BountyProgram, Competition
from app.db.models.coding import CodingSession
from app.db.models.conversation import Conversation, ConversationMessage
from app.db.models.entry import CompetitionEntry
from app.db.models.finding import Evidence, Finding
from app.db.models.memory import Memory
from app.db.models.model_provider import ModelProvider, ModelUsage
from app.db.models.project import Project, Repository
from app.db.models.research import FindingReview, ResearchScope, SecurityScan
from app.db.models.setting import SystemSetting
from app.db.models.skill import Skill, SkillExecution
from app.db.models.task import AgentMessage, Task, TaskDependency, TaskEvent, TaskStep
from app.db.models.telegram import TelegramUpdate
from app.db.models.tool import ToolRegistry
from app.db.models.user import ApiKey, User

__all__ = [
    "Agent",
    "AgentConfig",
    "AgentMessage",
    "AgentSkill",
    "AgentTool",
    "ApiKey",
    "Approval",
    "AuditLog",
    "BountyProgram",
    "CodingSession",
    "Competition",
    "CompetitionEntry",
    "Conversation",
    "ConversationMessage",
    "Evidence",
    "Finding",
    "FindingReview",
    "Memory",
    "ModelProvider",
    "ModelUsage",
    "Project",
    "Repository",
    "ResearchScope",
    "SecurityScan",
    "Skill",
    "SkillExecution",
    "SystemSetting",
    "Task",
    "TaskDependency",
    "TaskEvent",
    "TaskStep",
    "TelegramUpdate",
    "ToolRegistry",
    "User",
]
