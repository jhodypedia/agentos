"""Workspace confinement: traversal, symlinks, limits and search."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.core.exceptions import ConflictError, NotFoundError
from app.workspace.paths import (
    DEFAULT_MAX_FILE_BYTES,
    Workspace,
    WorkspaceError,
    WorkspaceQuotaExceeded,
    normalize_relative_path,
    validate_workspace_slug,
)


@pytest.fixture
async def workspace(tmp_path: Path) -> Workspace:
    return await Workspace.create(tmp_path / "ws")


@pytest.mark.parametrize(
    "raw",
    [
        "../escape.txt",
        "a/../../escape.txt",
        "/etc/passwd",
        "C:/Windows/system32",
        "c:\\temp\\file",
        "",
        "   ",
        ".",
    ],
)
def test_normalize_rejects_unsafe_paths(raw: str) -> None:
    with pytest.raises(WorkspaceError):
        normalize_relative_path(raw)


def test_normalize_accepts_relative_paths() -> None:
    assert normalize_relative_path("a/b/c.txt") == "a/b/c.txt"
    assert normalize_relative_path("./a//b/./c") == "a/b/c"
    assert normalize_relative_path("a\\b\\c") == "a/b/c"


def test_slug_validation() -> None:
    assert validate_workspace_slug("my-project") == "my-project"
    for bad in ("../x", "My Project", "-lead", "", "a" * 64):
        with pytest.raises(ConflictError):
            validate_workspace_slug(bad)


async def test_create_and_open(tmp_path: Path) -> None:
    ws = await Workspace.create(tmp_path / "a")
    assert ws.root.is_dir()
    reopened = await Workspace.open(tmp_path / "a")
    assert reopened.root == ws.root
    with pytest.raises(NotFoundError):
        await Workspace.open(tmp_path / "missing")


async def test_write_and_read_round_trip(workspace: Workspace) -> None:
    path = await workspace.write_bytes("src/app.py", b"print('hi')\n")
    assert workspace.relative(path) == "src/app.py"
    assert await workspace.read_bytes("src/app.py") == b"print('hi')\n"


async def test_write_rejects_traversal(workspace: Workspace) -> None:
    with pytest.raises(WorkspaceError):
        await workspace.write_bytes("../escape.txt", b"nope")


async def test_read_rejects_absolute_path(workspace: Workspace) -> None:
    with pytest.raises(WorkspaceError):
        await workspace.read_bytes("/etc/passwd")


async def test_symlink_escape_is_refused(workspace: Workspace, tmp_path: Path) -> None:
    secret = tmp_path / "outside.txt"
    secret.write_text("host secret")
    os.symlink(secret, workspace.root / "link.txt")
    with pytest.raises(WorkspaceError):
        await workspace.read_bytes("link.txt")


async def test_symlink_directory_escape_is_refused(workspace: Workspace, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f.txt").write_text("x")
    os.symlink(outside, workspace.root / "linkdir")
    with pytest.raises(WorkspaceError):
        await workspace.read_bytes("linkdir/f.txt")


async def test_read_missing_file(workspace: Workspace) -> None:
    with pytest.raises(NotFoundError):
        await workspace.read_bytes("nope.txt")


async def test_read_directory_is_refused(workspace: Workspace) -> None:
    (workspace.root / "d").mkdir()
    with pytest.raises(WorkspaceError):
        await workspace.read_bytes("d")


async def test_read_enforces_size_limit(workspace: Workspace) -> None:
    workspace.root.joinpath("big.bin").write_bytes(b"x" * 50)
    with pytest.raises(WorkspaceError):
        await workspace.read_bytes("big.bin", max_bytes=10)
    assert len(await workspace.read_bytes("big.bin")) == 50


async def test_write_enforces_size_limit(workspace: Workspace) -> None:
    with pytest.raises(WorkspaceError):
        await workspace.write_bytes("big.bin", b"x" * 10, max_bytes=5)
    assert DEFAULT_MAX_FILE_BYTES > 10


async def test_list_tree_is_relative_and_sorted(workspace: Workspace) -> None:
    await workspace.write_bytes("b.txt", b"b")
    await workspace.write_bytes("a/x.txt", b"a")
    entries = await workspace.list_tree()
    paths = [entry["path"] for entry in entries]
    assert "a" in paths and "a/x.txt" in paths and "b.txt" in paths
    assert all(not str(p).startswith("/") for p in paths)


async def test_list_tree_skips_noise_directories(workspace: Workspace) -> None:
    await workspace.write_bytes("node_modules/pkg/index.js", b"x")
    await workspace.write_bytes(".git/config", b"x")
    await workspace.write_bytes("src/main.py", b"x")
    paths = [entry["path"] for entry in await workspace.list_tree()]
    assert "src/main.py" in paths
    assert not any(p.startswith("node_modules") for p in paths)
    assert not any(p.startswith(".git") for p in paths)


async def test_list_tree_limits_entries(workspace: Workspace) -> None:
    for index in range(10):
        await workspace.write_bytes(f"f{index}.txt", b"x")
    assert len(await workspace.list_tree(max_entries=3)) == 3


async def test_list_tree_rejects_file_target(workspace: Workspace) -> None:
    await workspace.write_bytes("f.txt", b"x")
    with pytest.raises(WorkspaceError):
        await workspace.list_tree("f.txt")


async def test_delete_file_and_directory(workspace: Workspace) -> None:
    await workspace.write_bytes("a/b/c.txt", b"x")
    await workspace.delete("a/b/c.txt")
    assert not (workspace.root / "a/b/c.txt").exists()
    await workspace.delete("a")
    assert not (workspace.root / "a").exists()


async def test_delete_root_is_refused(workspace: Workspace) -> None:
    with pytest.raises(WorkspaceError):
        await workspace.delete(".")


async def test_search_finds_matches(workspace: Workspace) -> None:
    await workspace.write_bytes("src/one.py", b"import os\nprint('token')\n")
    await workspace.write_bytes("src/two.py", b"nothing here\n")
    matches = await workspace.search_text("token")
    assert len(matches) == 1
    assert matches[0]["path"] == "src/one.py"
    assert matches[0]["line"] == 2


async def test_search_respects_glob_and_limit(workspace: Workspace) -> None:
    await workspace.write_bytes("a.py", b"needle\n")
    await workspace.write_bytes("b.txt", b"needle\n")
    only_py = await workspace.search_text("needle", glob="**/*.py")
    assert [match["path"] for match in only_py] == ["a.py"]
    assert len(await workspace.search_text("needle", max_results=1)) == 1


async def test_search_skips_noise_directories(workspace: Workspace) -> None:
    await workspace.write_bytes("node_modules/x.js", b"needle\n")
    assert await workspace.search_text("needle") == []


async def test_search_skips_large_files(workspace: Workspace) -> None:
    workspace.root.joinpath("big.txt").write_bytes(b"needle\n" * 10_000)
    matches = await workspace.search_text("needle", max_file_bytes=100)
    assert matches == []


# ------------------------------------------------------------------ disk quota


async def test_usage_counts_regular_files(workspace: Workspace) -> None:
    await workspace.write_bytes("a.txt", b"12345")
    await workspace.write_bytes("nested/b.txt", b"1234567890")
    assert await workspace.usage_bytes() == 15


async def test_write_is_refused_when_it_would_cross_the_quota(workspace: Workspace) -> None:
    await workspace.write_bytes("a.txt", b"x" * 50, quota_bytes=100)
    with pytest.raises(WorkspaceQuotaExceeded) as excinfo:
        await workspace.write_bytes("b.txt", b"y" * 60, quota_bytes=100)
    assert excinfo.value.status_code == 413
    assert excinfo.value.code == "workspace_quota_exceeded"
    assert not workspace.root.joinpath("b.txt").exists()


async def test_rewriting_a_smaller_file_is_allowed_at_the_quota(workspace: Workspace) -> None:
    await workspace.write_bytes("a.txt", b"x" * 100, quota_bytes=100)
    path = await workspace.write_bytes("a.txt", b"x" * 10, quota_bytes=100)
    assert path.stat().st_size == 10
    assert await workspace.usage_bytes() == 10


async def test_check_quota_reports_bytes_in_use(workspace: Workspace) -> None:
    await workspace.write_bytes("a.txt", b"1234")
    assert await workspace.check_quota(0, quota_bytes=10) == 4
    assert await workspace.check_quota(6, quota_bytes=10) == 4
    with pytest.raises(WorkspaceQuotaExceeded):
        await workspace.check_quota(7, quota_bytes=10)


async def test_usage_early_exit_stops_counting_past_the_quota(workspace: Workspace) -> None:
    for index in range(5):
        await workspace.write_bytes(f"f{index}.bin", b"z" * 100)
    used = await workspace.usage_bytes(quota_bytes=150)
    assert used > 150
    assert used < 500
