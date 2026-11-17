"""Workspace confinement: the one place a path becomes safe to touch.

Every filesystem, terminal and git operation an agent performs resolves its
targets through :class:`Workspace`. The rule is deliberately narrow: a path is
allowed only if, after resolving symlinks, it is still inside the workspace
root. Anything else — ``../`` traversal, an absolute path elsewhere, a symlink
pointing out, a device node — is refused before any I/O happens.

Keeping this in one module means the tools never re-implement the check, and a
tool that forgets to call it is a bug visible in review rather than a hole
buried in a regex.
"""

from __future__ import annotations

import asyncio
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from app.core.exceptions import ConflictError, NotFoundError, PermissionDeniedError

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_WINDOWS_DRIVE_RE = re.compile(r"^[a-zA-Z]:")

DEFAULT_MAX_FILE_BYTES = 1_000_000
DEFAULT_MAX_TREE_ENTRIES = 2_000
# Fallback quota when the caller does not pass one. 5 GiB matches the seeded
# `execution.workspace_quota_mb` setting.
DEFAULT_QUOTA_BYTES = 5 * 1024 * 1024 * 1024


class WorkspaceError(PermissionDeniedError):
    """A path or operation was refused by workspace policy."""


class WorkspaceQuotaExceeded(WorkspaceError):
    """The workspace is at or would exceed its disk quota.

    A distinct type so a caller can report "quota" rather than "forbidden": the
    agent should be told to delete files, not that it lacks permission.
    """

    status_code = 413
    code = "workspace_quota_exceeded"
    message = "The workspace disk quota is exhausted"


def validate_workspace_slug(slug: str) -> str:
    """Workspace names become directory names, so they are validated strictly."""
    if not _SLUG_RE.match(slug):
        raise ConflictError(
            "A workspace name must be lowercase alphanumeric with hyphens, "
            "start with a letter or digit and be at most 63 characters"
        )
    if slug in {".", ".."}:
        raise ConflictError("Invalid workspace name")
    return slug


def normalize_relative_path(raw: str) -> str:
    """Normalise an agent-supplied path into a relative, posix-style path.

    Absolute paths, Windows drive letters and any component that walks upwards
    are rejected outright rather than rewritten: silently clamping ``..`` would
    make a tool appear to succeed while acting on a different file.
    """
    if not raw or not raw.strip():
        raise WorkspaceError("A path is required")
    text = raw.replace("\\", "/").strip()
    if _WINDOWS_DRIVE_RE.match(text):
        raise WorkspaceError(f"Absolute paths are not allowed: '{raw}'")
    if text.startswith("/"):
        raise WorkspaceError(f"Absolute paths are not allowed: '{raw}'")

    parts: list[str] = []
    for part in text.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            raise WorkspaceError(f"Path traversal is not allowed: '{raw}'")
        parts.append(part)
    if not parts:
        raise WorkspaceError(f"Path does not name a file: '{raw}'")
    return "/".join(parts)


@dataclass(slots=True, frozen=True)
class Workspace:
    """A confined directory tree an agent may read and write."""

    root: Path

    @classmethod
    async def create(cls, root: Path) -> Workspace:
        """Create (or adopt) the workspace root directory."""
        await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
        resolved = await asyncio.to_thread(root.resolve)
        if resolved == Path(resolved.anchor):
            raise WorkspaceError("A workspace may not be a filesystem root")
        return cls(root=resolved)

    @classmethod
    async def open(cls, root: Path) -> Workspace:
        """Adopt an existing workspace; missing roots are an error, not created."""
        if not await asyncio.to_thread(root.exists):
            raise NotFoundError(f"Workspace '{root}' does not exist")
        resolved = await asyncio.to_thread(root.resolve)
        return cls(root=resolved)

    def relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    async def resolve(self, raw: str, *, must_exist: bool = False) -> Path:
        """Resolve an agent-supplied path, refusing anything outside the root.

        Symlinks are resolved before the containment check, so a link inside the
        workspace that points at ``/etc/passwd`` is rejected — checking the
        unresolved path would pass and then read the host file.
        """
        if raw.strip() in {"", ".", "./"}:
            return self.root
        relative = normalize_relative_path(raw)
        candidate = self.root / relative
        resolved = await asyncio.to_thread(candidate.resolve)

        if resolved != self.root and self.root not in resolved.parents:
            raise WorkspaceError(f"Path escapes the workspace: '{raw}'")

        if must_exist and not await asyncio.to_thread(resolved.exists):
            raise NotFoundError(f"'{relative}' does not exist in the workspace")
        return resolved

    async def read_bytes(self, raw: str, *, max_bytes: int = DEFAULT_MAX_FILE_BYTES) -> bytes:
        path = await self.resolve(raw, must_exist=True)
        if not await asyncio.to_thread(path.is_file):
            raise WorkspaceError(f"'{raw}' is not a regular file")

        size = await asyncio.to_thread(lambda: path.stat().st_size)
        if size > max_bytes:
            raise WorkspaceError(f"'{raw}' is {size} bytes, larger than the {max_bytes}-byte limit")
        return await asyncio.to_thread(path.read_bytes)

    async def write_bytes(
        self,
        raw: str,
        data: bytes,
        *,
        max_bytes: int = DEFAULT_MAX_FILE_BYTES,
        quota_bytes: int = DEFAULT_QUOTA_BYTES,
    ) -> Path:
        if len(data) > max_bytes:
            raise WorkspaceError(f"Refusing to write {len(data)} bytes (limit {max_bytes})")
        path = await self.resolve(raw)
        # Only the *increase* counts against the quota: rewriting a file with
        # smaller content must not be refused because the tree is already at its
        # limit.
        existing = 0
        if await asyncio.to_thread(path.is_file):
            existing = await asyncio.to_thread(lambda: path.stat().st_size)
        await self.check_quota(max(0, len(data) - existing), quota_bytes=quota_bytes)
        await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_bytes, data)
        return path

    async def usage_bytes(self, *, quota_bytes: int | None = None) -> int:
        """Total bytes below the root, stopping early once ``quota_bytes`` is passed.

        The early exit matters: a workspace already over quota does not need an
        exact figure, only the knowledge that it is over.
        """
        return await asyncio.to_thread(_tree_bytes, self.root, quota_bytes)

    async def check_quota(
        self, incoming_bytes: int = 0, *, quota_bytes: int = DEFAULT_QUOTA_BYTES
    ) -> int:
        """Refuse work that would push the workspace past its quota.

        Returns the bytes in use. Raises :class:`WorkspaceQuotaExceeded` when
        ``usage + incoming_bytes`` exceeds ``quota_bytes``.
        """
        used = await self.usage_bytes(quota_bytes=quota_bytes)
        if used + incoming_bytes > quota_bytes:
            from app.core.metrics import WORKSPACE_QUOTA_REJECTED

            WORKSPACE_QUOTA_REJECTED.inc()
            raise WorkspaceQuotaExceeded(
                f"Workspace is using {_human(used)} of its {_human(quota_bytes)} quota; "
                f"this operation would add {_human(incoming_bytes)}. Delete files or raise "
                "`execution.workspace_quota_mb`."
            )
        return used

    async def delete(self, raw: str) -> Path:
        path = await self.resolve(raw, must_exist=True)
        if path == self.root:
            raise WorkspaceError("The workspace root cannot be deleted")
        if await asyncio.to_thread(path.is_dir):
            await asyncio.to_thread(_remove_tree, path)
        else:
            await asyncio.to_thread(path.unlink)
        return path

    async def list_tree(
        self, raw: str = ".", *, max_entries: int = DEFAULT_MAX_TREE_ENTRIES
    ) -> list[dict[str, object]]:
        base = await self.resolve(raw)
        if not await asyncio.to_thread(base.is_dir):
            raise WorkspaceError(f"'{raw}' is not a directory")
        return await asyncio.to_thread(_walk_tree, base, self.root, max_entries)

    async def search_text(
        self,
        pattern: str,
        *,
        glob: str = "**/*",
        max_results: int = 200,
        max_file_bytes: int = 400_000,
    ) -> list[dict[str, object]]:
        return await asyncio.to_thread(
            _search, self.root, pattern, glob, max_results, max_file_bytes
        )


def _remove_tree(path: Path) -> None:
    import shutil

    shutil.rmtree(path)


def _human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def _tree_bytes(root: Path, limit: int | None) -> int:
    """Sum regular-file sizes below ``root``, stopping early once ``limit`` is passed."""
    total = 0
    for current, _dirnames, filenames in os.walk(root):
        current_path = Path(current)
        for name in filenames:
            try:
                info = (current_path / name).lstat()
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            total += info.st_size
            if limit is not None and total > limit:
                return total
    return total


_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", ".mypy_cache", ".ruff_cache"}


def _walk_tree(base: Path, root: Path, max_entries: int) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for current, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        current_path = Path(current)
        for name in dirnames:
            target = current_path / name
            entries.append(
                {
                    "path": target.relative_to(root).as_posix(),
                    "type": "dir",
                    "size": 0,
                }
            )
        for name in sorted(filenames):
            target = current_path / name
            try:
                size = target.stat().st_size
            except OSError:
                size = 0
            entries.append(
                {
                    "path": target.relative_to(root).as_posix(),
                    "type": "file",
                    "size": size,
                }
            )
        if len(entries) >= max_entries:
            return entries[:max_entries]
    return entries


def _search(
    root: Path,
    pattern: str,
    glob: str,
    max_results: int,
    max_file_bytes: int,
) -> list[dict[str, object]]:
    compiled = re.compile(pattern)
    results: list[dict[str, object]] = []
    for path in sorted(root.glob(glob)):
        if len(results) >= max_results:
            break
        if not path.is_file() or any(part in _SKIP_DIRS for part in path.parts):
            continue
        try:
            if path.stat().st_size > max_file_bytes:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if compiled.search(line):
                results.append(
                    {
                        "path": path.relative_to(root).as_posix(),
                        "line": number,
                        "text": line.strip()[:400],
                    }
                )
                if len(results) >= max_results:
                    break
    return results
