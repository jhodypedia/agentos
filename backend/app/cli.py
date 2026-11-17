"""Operator CLI: bootstrap accounts and seed safe defaults.

Usage:
    uv run agentos create-admin --email admin@example.com
    uv run agentos seed
    uv run agentos seed-agents --provider <provider-id> --model gpt-4o-mini
    uv run agentos seed-skills
    uv run agentos rotate-encryption-key --dry-run
    uv run agentos scan-secrets --path /data/workspaces
    uv run agentos prune --dry-run
    uv run agentos worker
    uv run agentos bot
    uv run agentos queue --status
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from datetime import UTC, datetime
from typing import Any

from cryptography.fernet import InvalidToken
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.security import rotate_secret
from app.db.enums import UserRole
from app.db.models import SystemSetting
from app.db.session import dispose_engine, get_session_factory
from app.orchestrator.specialists import load_overrides, sync_specialists
from app.services.agents import sync_tool_registry
from app.services.skills import sync_builtin_skills, sync_from_directory
from app.services.users import create_user, get_user_by_email
from app.tools.builtin import BUILTIN_TOOLS

DEFAULT_SETTINGS: list[dict[str, Any]] = [
    {
        "key": "platform.registration_enabled",
        "value": {"value": False},
        "description": "Allow self-service account registration",
    },
    {
        "key": "security.require_telegram_allowlist",
        "value": {"value": True},
        "description": "Reject Telegram users that are not on the allowlist",
    },
    {
        "key": "security.approvals_required",
        "value": {
            "value": [
                "submit_competition",
                "external_report",
                "deploy",
                "run_high_risk_tool",
                "spend_budget",
            ]
        },
        "description": "Actions that always require human approval",
    },
    {
        "key": "execution.sandbox_enabled",
        "value": {"value": True},
        "description": "Run untrusted tool code inside Docker sandboxes",
    },
    {
        "key": "execution.workspace_quota_mb",
        "value": {"value": 5120},
        "description": "Per-workspace disk quota in megabytes",
    },
    {
        "key": "orchestrator.max_parallel_tasks",
        "value": {"value": 4},
        "description": "Maximum number of concurrently executing tasks",
    },
    {
        "key": "orchestrator.default_task_timeout_seconds",
        "value": {"value": 3600},
        "description": "Default wall-clock timeout for a task",
    },
    {
        "key": "orchestrator.default_task_budget_usd",
        "value": {"value": 5.0},
        "description": "Default per-task model spend cap in USD",
    },
    {
        "key": "memory.retention_days",
        "value": {"value": 365},
        "description": "Retention window for episodic memories",
    },
    {
        "key": "ui.theme",
        "value": {"value": "dark"},
        "description": "Default dashboard theme",
    },
]


async def _create_admin(email: str, password: str, display_name: str) -> None:
    settings = get_settings()
    factory = get_session_factory(settings)
    async with factory() as db:
        existing = await get_user_by_email(db, email)
        if existing is not None:
            if existing.role != UserRole.OWNER:
                existing.role = UserRole.OWNER
                existing.is_active = True
                await db.commit()
                print(f"Promoted existing user {existing.email} to owner")
            else:
                print(f"Owner account {existing.email} already exists")
            return
        user = await create_user(
            db,
            email=email,
            password=password,
            display_name=display_name,
            role=UserRole.OWNER,
        )
        await db.commit()
        print(f"Created owner account: {user.email} ({user.id})")


async def _seed(db: AsyncSession) -> tuple[int, int]:
    created = 0
    updated = 0
    for entry in DEFAULT_SETTINGS:
        result = await db.execute(select(SystemSetting).where(SystemSetting.key == entry["key"]))
        setting = result.scalar_one_or_none()
        if setting is None:
            db.add(SystemSetting(**entry))
            created += 1
        else:
            setting.value = entry["value"]
            setting.description = entry["description"]
            updated += 1
    return created, updated


async def _run_seed() -> None:
    settings = get_settings()
    factory = get_session_factory(settings)
    async with factory() as db:
        created, updated = await _seed(db)
        await db.commit()
    print(f"System settings seeded: {created} created, {updated} updated")

    async with factory() as db:
        tools = await sync_tool_registry(db, [tool() for tool in BUILTIN_TOOLS])
        await db.commit()
    print(f"Tool registry synced: {tools['created']} created, {tools['updated']} updated")


async def _run_seed_agents(args: argparse.Namespace) -> None:
    from uuid import UUID

    from app.db.models import ModelProvider

    settings = get_settings()
    factory = get_session_factory(settings)
    async with factory() as db:
        provider_id: UUID | None = None
        model = args.model
        if args.provider:
            try:
                provider_id = UUID(args.provider)
            except ValueError as exc:
                raise SystemExit("--provider must be a provider UUID") from exc
            provider = await db.get(ModelProvider, provider_id)
            if provider is None:
                raise SystemExit(f"No model provider with id {provider_id}")
            model = model or provider.default_model
        elif args.use_default_provider:
            result = await db.execute(
                select(ModelProvider)
                .where(ModelProvider.enabled.is_(True))
                .order_by(ModelProvider.created_at.asc())
                .limit(1)
            )
            provider = result.scalar_one_or_none()
            if provider is not None:
                provider_id = provider.id
                model = model or provider.default_model

        overrides = load_overrides(args.config)
        report = await sync_specialists(
            db,
            overrides=overrides,
            default_provider_id=provider_id,
            default_model=model,
            overwrite=args.overwrite,
        )
        await db.commit()

    print(
        "Specialist agents synced: "
        f"{len(report.created)} created, {len(report.updated)} updated, "
        f"{len(report.unchanged)} unchanged"
    )
    for slug, missing in sorted(report.skipped_tools.items()):
        print(f"  {slug}: unregistered tools skipped: {', '.join(missing)}")
    if provider_id is None:
        print(
            "No model provider assigned. Set one per agent, or re-run with "
            "--provider/--model (or --use-default-provider)."
        )


async def _run_seed_skills(args: argparse.Namespace) -> None:
    """Copy the shipped skills into the managed root and register them."""
    settings = get_settings()
    factory = get_session_factory(settings)
    async with factory() as db:
        report = await sync_builtin_skills(db)
        await db.commit()

        extra: list[Any] = []
        for extra_path in getattr(args, "path", None) or []:
            from pathlib import Path

            extra_report = await sync_from_directory(db, Path(extra_path), source="local")
            await db.commit()
            extra.append((extra_path, extra_report))

    print(
        "Skills synced: "
        f"{len(report.installed)} installed, {len(report.unchanged)} unchanged, "
        f"{len(report.failed)} failed"
    )
    for path, message in sorted(report.failed.items()):
        print(f"  failed: {path}: {message}")
    for path, extra_report in extra:
        print(
            f"  {path}: {len(extra_report.installed)} installed, "
            f"{len(extra_report.unchanged)} unchanged, {len(extra_report.failed)} failed"
        )
        for failed_path, message in sorted(extra_report.failed.items()):
            print(f"    failed: {failed_path}: {message}")


async def _run_rotate_keys(args: argparse.Namespace) -> None:
    from app.db.models import ModelProvider
    from app.services.maintenance import rotate_encryption_keys

    settings = get_settings()
    if args.dry_run:
        # Report what a real run would change, not how many keys exist: "1
        # stored key would be re-encrypted" was printed for a key that is
        # already current, which reads like the run is not idempotent.
        factory = get_session_factory(settings)
        pending = 0
        current = 0
        unreadable = 0
        async with factory() as db:
            result = await db.execute(select(ModelProvider))
            for provider in result.scalars():
                if not provider.api_key_encrypted:
                    continue
                try:
                    _updated, changed = rotate_secret(provider.api_key_encrypted, settings=settings)
                except (ValueError, InvalidToken):
                    unreadable += 1
                    continue
                if changed:
                    pending += 1
                else:
                    current += 1
        print(
            f"Dry run: {pending} key(s) would be re-encrypted, "
            f"{current} already current, {unreadable} unreadable"
        )
        return

    factory = get_session_factory(settings)
    async with factory() as db:
        report = await rotate_encryption_keys(db, settings)
        await db.commit()
    print(
        "Encryption key rotation: "
        f"{report['rotated']} re-encrypted, {report['unchanged']} already current, "
        f"{report['failed']} unreadable"
    )
    if report["failed"]:
        print(
            "Unreadable rows were left untouched: the key that wrote them is not in "
            "ENCRYPTION_KEY or ENCRYPTION_KEY_PREVIOUS."
        )
    if report["rotated"]:
        print("The old key can be removed from ENCRYPTION_KEY_PREVIOUS once this is verified.")


async def _run_scan_secrets(args: argparse.Namespace) -> None:
    from pathlib import Path

    from app.services.maintenance import scan_path

    targets = [Path(item) for item in (args.path or [args.root])]
    findings: list[Any] = []
    for target in targets:
        findings.extend(await asyncio.to_thread(scan_path, target))
    if not findings:
        print(f"No credential patterns found in {len(targets)} path(s)")
        return
    for finding in findings:
        print(
            f"{finding.path}:{finding.line}: {finding.rule} ({finding.severity}) {finding.preview}"
        )
    print(f"{len(findings)} finding(s)")
    if args.fail_on_findings:
        raise SystemExit(1)


async def _run_queue(args: argparse.Namespace) -> None:
    from app.services import queue as queue_service

    factory = get_session_factory(get_settings())
    async with factory() as db:
        if args.reap:
            reaped = await queue_service.reap_expired(db)
            print(f"Requeued {reaped} task(s) with an expired lease")
        if args.status or not args.reap:
            from sqlalchemy import func, select

            from app.db.enums import TaskStatus
            from app.db.models import Task

            counts = {
                str(getattr(value, "value", value)): int(count)
                for value, count in await db.execute(
                    select(Task.status, func.count()).select_from(Task).group_by(Task.status)
                )
            }
            claimable = await queue_service.queue_depth(db)
            now = datetime.now(UTC)
            leased = int(
                await db.scalar(
                    select(func.count()).select_from(Task).where(Task.lease_expires_at.is_not(None))
                )
                or 0
            )
            expired = int(
                await db.scalar(
                    select(func.count())
                    .select_from(Task)
                    .where(
                        Task.lease_expires_at.is_not(None),
                        Task.lease_expires_at < now,
                        Task.status.in_((TaskStatus.RUNNING, TaskStatus.QUEUED)),
                    )
                )
                or 0
            )
            print(f"Claimable: {claimable}")
            print(f"Leased: {leased}")
            print(f"Expired leases awaiting reaping: {expired}")
            for status in sorted(counts):
                print(f"  {status}: {counts[status]}")


async def _run_prune(args: argparse.Namespace) -> None:
    from app.services.maintenance import prune_old_records

    settings = get_settings()
    factory = get_session_factory(settings)
    async with factory() as db:
        report = await prune_old_records(db, settings)
        if args.dry_run:
            await db.rollback()
        else:
            await db.commit()
    prefix = "Dry run: " if args.dry_run else ""
    print(
        f"{prefix}pruned {report.task_events} task event(s), "
        f"{report.audit_logs} audit row(s), {report.model_usage} usage row(s)"
    )
    for name, cutoff in sorted(report.cutoff.items()):
        print(f"  {name}: older than {cutoff.isoformat()}")


def _resolve_password(explicit: str | None) -> str:
    if explicit:
        return explicit
    env_password = get_settings().bootstrap_admin_password
    if env_password is not None:
        return env_password.get_secret_value()
    if not sys.stdin.isatty():
        raise SystemExit("No password supplied. Pass --password or set BOOTSTRAP_ADMIN_PASSWORD.")
    first = getpass.getpass("Password: ")
    second = getpass.getpass("Repeat password: ")
    if first != second:
        raise SystemExit("Passwords do not match")
    return first


async def _dispatch(args: argparse.Namespace) -> None:
    configure_logging(get_settings())
    try:
        if args.command == "create-admin":
            email = args.email or get_settings().bootstrap_admin_email
            if not email:
                raise SystemExit("--email or BOOTSTRAP_ADMIN_EMAIL is required")
            await _create_admin(email, _resolve_password(args.password), args.name)
        elif args.command == "seed":
            await _run_seed()
        elif args.command == "seed-agents":
            await _run_seed_agents(args)
        elif args.command == "seed-skills":
            await _run_seed_skills(args)
        elif args.command == "rotate-encryption-key":
            await _run_rotate_keys(args)
        elif args.command == "scan-secrets":
            await _run_scan_secrets(args)
        elif args.command == "prune":
            await _run_prune(args)
        elif args.command == "worker":
            from app.worker import run_worker

            await run_worker()
        elif args.command == "bot":
            from app.bot import run_bot

            await run_bot()
        elif args.command == "queue":
            await _run_queue(args)
        elif args.command == "version":
            cfg = get_settings()
            print(f"{cfg.app_name} backend {cfg.app_version} ({cfg.environment})")
    finally:
        await dispose_engine()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentos", description="AgentOS operator CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    admin = sub.add_parser("create-admin", help="Create or promote the owner account")
    admin.add_argument("--email")
    admin.add_argument("--password")
    admin.add_argument("--name", default="Operator")

    sub.add_parser("seed", help="Upsert default system settings")

    agents = sub.add_parser("seed-agents", help="Create the built-in specialist agents")
    agents.add_argument("--provider", help="Model provider UUID to assign by default")
    agents.add_argument("--model", help="Model name to assign by default")

    skills = sub.add_parser(
        "seed-skills", help="Install the shipped skills and rescan the skills root"
    )
    skills.add_argument(
        "--path", help="Additional directory to scan for SKILL.md files (repeatable)"
    )
    agents.add_argument(
        "--use-default-provider",
        action="store_true",
        help="Assign the oldest enabled provider when --provider is omitted",
    )
    agents.add_argument(
        "--config",
        help="YAML/JSON file overriding specialist definitions by slug",
    )
    agents.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing specialist definitions (operator edits are lost)",
    )
    rotate = sub.add_parser(
        "rotate-encryption-key",
        help="Re-encrypt stored provider keys under the current ENCRYPTION_KEY",
    )
    rotate.add_argument("--dry-run", action="store_true", help="Report without writing")

    sub.add_parser("worker", help="Run the background task worker")

    sub.add_parser("bot", help="Run the Telegram notification loop")

    queue = sub.add_parser("queue", help="Inspect or recover the task queue")
    queue.add_argument(
        "--status",
        action="store_true",
        help="Print claimable, running and lease-expired task counts",
    )
    queue.add_argument(
        "--reap",
        action="store_true",
        help="Return tasks whose worker lease expired to the queue",
    )

    scan = sub.add_parser("scan-secrets", help="Scan a directory for credential patterns")
    scan.add_argument("--path", action="append", help="Directory to scan (repeatable)")
    scan.add_argument("--root", default=".", help="Directory scanned when --path is omitted")
    scan.add_argument(
        "--fail-on-findings",
        action="store_true",
        help="Exit non-zero when anything is found (for CI)",
    )

    prune = sub.add_parser("prune", help="Delete rows past their retention window")
    prune.add_argument("--dry-run", action="store_true", help="Report without deleting")

    sub.add_parser("version", help="Print the backend version")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    asyncio.run(_dispatch(args))


if __name__ == "__main__":
    main()
