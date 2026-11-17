from __future__ import annotations

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

import app.db.models  # noqa: F401
from app.db.base import Base

EXPECTED_TABLES = {
    "agent_configs",
    "agent_messages",
    "agent_skills",
    "agent_tools",
    "agents",
    "api_keys",
    "approvals",
    "audit_logs",
    "bounty_programs",
    "coding_sessions",
    "competition_entries",
    "competitions",
    "conversation_messages",
    "conversations",
    "evidence",
    "findings",
    "finding_reviews",
    "research_scopes",
    "security_scans",
    "memories",
    "model_providers",
    "model_usage",
    "projects",
    "repositories",
    "skill_executions",
    "skills",
    "system_settings",
    "task_dependencies",
    "task_events",
    "task_steps",
    "tasks",
    "tool_registry",
    "users",
    "telegram_updates",
}


def test_all_required_tables_exist() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_schema_compiles_for_postgres() -> None:
    dialect = postgresql.dialect()
    for table in Base.metadata.sorted_tables:
        ddl = str(CreateTable(table).compile(dialect=dialect))
        assert f"CREATE TABLE {table.name}" in ddl
        for index in table.indexes:
            assert "CREATE" in str(CreateIndex(index).compile(dialect=dialect))


def test_every_fk_target_exists() -> None:
    for table in Base.metadata.tables.values():
        for fk in table.foreign_keys:
            target = fk.target_fullname.split(".")[0]
            reference = f"{table.name}.{fk.parent.name} -> {fk.target_fullname}"
            assert target in Base.metadata.tables, reference


def test_money_columns_use_numeric() -> None:
    from decimal import Decimal

    tasks = Base.metadata.tables["tasks"]
    assert tasks.c.budget_usd.type.python_type is Decimal
    assert tasks.c.cost_usd.type.python_type is Decimal
