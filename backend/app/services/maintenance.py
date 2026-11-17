"""Operator maintenance: secret scanning, key rotation and retention pruning.

Three jobs an operator has to be able to run without a database client:

* :func:`scan_text` / :func:`scan_path` — find credentials accidentally left in a
  workspace or a config dump, and report file, line and rule without printing
  the secret itself.
* :func:`rotate_encryption_keys` — re-encrypt stored provider keys under a new
  Fernet key so the old one can be retired.
* :func:`prune_old_records` — enforce the retention windows configured on the
  settings object.

The scanner is pattern based. It is a safety net for honest mistakes, not a
defence against a determined attacker: it cannot recognise a secret that looks
like ordinary text, and it deliberately reports the location rather than the
value so a scan result can be shared in a ticket.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from cryptography.fernet import InvalidToken
from sqlalchemy import CursorResult, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.security import rotate_secret

# Deliberately conservative: a false positive costs a developer ten seconds, a
# false negative can cost a leaked production credential.
_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "high"),
    (
        "private_key_block",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"),
        "critical",
    ),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), "high"),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b"), "high"),
    ("github_token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b"), "high"),
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b"), "high"),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"), "high"),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), "high"),
    ("stripe_key", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b"), "high"),
    ("telegram_bot_token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"), "medium"),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
        "medium",
    ),
    (
        "generic_assignment",
        re.compile(
            r"""(?i)\b(?:api[_-]?key|secret[_-]?key|access[_-]?token|password)\b"""
            r"""\s*[:=]\s*["']([^"'\s]{16,})["']"""
        ),
        "medium",
    ),
    (
        "database_url_with_password",
        re.compile(r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://[^\s:/@]+:[^\s@/]+@"),
        "high",
    ),
)

# Files whose content is binary or generated noise; scanning them wastes time and
# produces matches inside compressed blobs.
_SKIP_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        "dist",
        "build",
        ".next",
        "target",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
    }
)
_SKIP_SUFFIXES = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".ico",
        ".pdf",
        ".zip",
        ".gz",
        ".tar",
        ".whl",
        ".so",
        ".dylib",
        ".dll",
        ".pyc",
        ".lock",
    }
)
_MAX_SCAN_BYTES = 2_000_000


@dataclass(frozen=True, slots=True)
class Finding:
    path: str
    line: int
    rule: str
    severity: str
    preview: str


def scan_text(text: str, *, path: str = "<text>") -> list[Finding]:
    """Find credential-shaped strings, reporting the line but never the value."""
    findings: list[Finding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        for rule, pattern, severity in _PATTERNS:
            match = pattern.search(line)
            if match is None:
                continue
            findings.append(
                Finding(
                    path=path,
                    line=number,
                    rule=rule,
                    severity=severity,
                    preview=_preview(line, match.start(), match.end()),
                )
            )
            break  # one finding per line keeps the report readable
    return findings


def _preview(line: str, start: int, end: int) -> str:
    """Redact the matched span, keeping enough context to find the line."""
    head = line[:start].strip()
    tail = line[end:].strip()
    head = head[-24:] if len(head) > 24 else head
    tail = tail[:16] if len(tail) > 16 else tail
    masked = "*" * min(8, max(4, end - start))
    return f"{head}{masked}{tail}".strip()


def scan_path(root: Path, *, max_findings: int = 200) -> list[Finding]:
    """Walk ``root`` and scan every likely-text file."""
    findings: list[Finding] = []
    if not root.exists():
        return findings
    for candidate in sorted(root.rglob("*")):
        if len(findings) >= max_findings:
            break
        if any(part in _SKIP_DIRS for part in candidate.parts):
            continue
        if not candidate.is_file() or candidate.suffix.lower() in _SKIP_SUFFIXES:
            continue
        try:
            if candidate.stat().st_size > _MAX_SCAN_BYTES:
                continue
            raw = candidate.read_bytes()
        except OSError:
            continue
        if b"\x00" in raw[:4096]:
            continue
        text = raw.decode("utf-8", errors="replace")
        findings.extend(scan_text(text, path=str(candidate)))
        if len(findings) >= max_findings:
            break
    return findings[:max_findings]


async def scan_path_async(root: Path, *, max_findings: int = 200) -> list[Finding]:
    return await asyncio.to_thread(scan_path, root, max_findings=max_findings)


async def rotate_encryption_keys(db: AsyncSession, settings: Settings) -> dict[str, int]:
    """Re-encrypt every stored secret under the primary Fernet key."""
    from app.db.models import ModelProvider

    providers = (await db.execute(select(ModelProvider))).scalars().all()
    rotated = 0
    unchanged = 0
    failed = 0
    for provider in providers:
        if not provider.api_key_encrypted:
            continue
        try:
            updated, changed = rotate_secret(provider.api_key_encrypted, settings=settings)
        except (ValueError, InvalidToken):
            # No configured key can read this row. Leaving it alone and counting
            # it is the honest outcome: rewriting it would destroy the value.
            failed += 1
            continue
        if changed:
            provider.api_key_encrypted = updated
            rotated += 1
        else:
            unchanged += 1
    await db.flush()
    return {"rotated": rotated, "unchanged": unchanged, "failed": failed}


@dataclass(frozen=True, slots=True)
class PruneReport:
    task_events: int
    audit_logs: int
    model_usage: int
    cutoff: dict[str, datetime]


async def prune_old_records(db: AsyncSession, settings: Settings) -> PruneReport:
    """Delete rows older than the configured retention windows.

    A window of 0 means "keep forever" — an explicit operator choice, and the
    default for the audit log on a fresh install.
    """
    from app.db.models import AuditLog, ModelUsage, TaskEvent

    now = datetime.now(UTC)
    cutoffs = {
        "task_events": now - timedelta(days=settings.retention_task_events_days),
        "audit_logs": now - timedelta(days=settings.retention_audit_days),
        "model_usage": now - timedelta(days=settings.retention_model_usage_days),
    }

    deleted = {"task_events": 0, "audit_logs": 0, "model_usage": 0}
    if settings.retention_task_events_days > 0:
        result = await db.execute(
            delete(TaskEvent).where(TaskEvent.created_at < cutoffs["task_events"])
        )
        deleted["task_events"] = int(cast("CursorResult[Any]", result).rowcount or 0)
    if settings.retention_audit_days > 0:
        result = await db.execute(
            delete(AuditLog).where(AuditLog.created_at < cutoffs["audit_logs"])
        )
        deleted["audit_logs"] = int(cast("CursorResult[Any]", result).rowcount or 0)
    if settings.retention_model_usage_days > 0:
        result = await db.execute(
            delete(ModelUsage).where(ModelUsage.created_at < cutoffs["model_usage"])
        )
        deleted["model_usage"] = int(cast("CursorResult[Any]", result).rowcount or 0)

    await db.flush()
    return PruneReport(
        task_events=deleted["task_events"],
        audit_logs=deleted["audit_logs"],
        model_usage=deleted["model_usage"],
        cutoff=cutoffs,
    )
