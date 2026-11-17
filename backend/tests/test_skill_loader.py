"""Unit tests for SKILL.md parsing, validation and discovery."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.skills.loader import (
    SkillError,
    copy_skill_directory,
    discover_skills,
    load_skill_directory,
    parse_skill_file,
    skill_prompt,
    split_frontmatter,
    validate_frontmatter,
)

VALID = """---
name: Deploy Service
description: Deploy a service safely.
version: 2.1.0
allowed-tools: [http_get, now]
---

# Steps
1. Check health.
2. Deploy.
"""


def _write(directory: Path, text: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    entry = directory / "SKILL.md"
    entry.write_text(text, encoding="utf-8")
    return entry


def test_split_frontmatter_returns_metadata_and_body() -> None:
    frontmatter, body = split_frontmatter(VALID)
    assert frontmatter["name"] == "Deploy Service"
    assert frontmatter["version"] == "2.1.0"
    assert body.startswith("# Steps")


def test_split_frontmatter_requires_leading_block() -> None:
    with pytest.raises(SkillError, match="must start with a YAML frontmatter"):
        split_frontmatter("# No frontmatter\n")


def test_split_frontmatter_requires_closing_delimiter() -> None:
    with pytest.raises(SkillError, match="not closed"):
        split_frontmatter("---\nname: x\n\nbody\n")


def test_split_frontmatter_rejects_non_mapping() -> None:
    with pytest.raises(SkillError, match="must be a mapping"):
        split_frontmatter("---\n- a\n- b\n---\nbody\n")


def test_split_frontmatter_rejects_invalid_yaml() -> None:
    with pytest.raises(SkillError, match="not valid YAML"):
        split_frontmatter("---\nname: [unclosed\n---\nbody\n")


def test_validate_uses_directory_name_as_default_slug() -> None:
    fields = validate_frontmatter(
        {"name": "Thing", "description": "Does a thing."}, default_slug="my-thing"
    )
    assert fields["slug"] == "my-thing"
    assert fields["version"] == "1.0.0"


def test_validate_normalises_a_derived_slug() -> None:
    fields = validate_frontmatter({"name": "X", "description": "Y"}, default_slug="My_Weird Slug!!")
    assert fields["slug"] == "my-weird-slug"


def test_validate_does_not_silently_repair_an_explicit_slug() -> None:
    with pytest.raises(SkillError, match="invalid"):
        validate_frontmatter({"slug": "My_Weird Slug!!", "name": "X", "description": "Y"})


@pytest.mark.parametrize("slug", ["-leading", "trailing-", "double--hyphen", "UPPER"])
def test_validate_rejects_invalid_slugs(slug: str) -> None:
    with pytest.raises(SkillError):
        validate_frontmatter({"slug": slug, "name": "X", "description": "Y"})


def test_validate_requires_name_and_description() -> None:
    with pytest.raises(SkillError, match="Missing required frontmatter field 'description'"):
        validate_frontmatter({"slug": "x", "name": "X"})


def test_validate_rejects_non_semver() -> None:
    with pytest.raises(SkillError, match="not semver"):
        validate_frontmatter({"slug": "x", "name": "X", "description": "Y", "version": "v1"})


def test_validate_rejects_overlong_description() -> None:
    with pytest.raises(SkillError, match="longer than"):
        validate_frontmatter({"slug": "x", "name": "X", "description": "d" * 2001})


def test_parse_skill_file(tmp_path: Path) -> None:
    entry = _write(tmp_path / "deploy-service", VALID)
    document = parse_skill_file(entry, default_slug="deploy-service")
    assert document.slug == "deploy-service"
    assert document.version == "2.1.0"
    assert document.allowed_tools == ["http_get", "now"]
    assert document.content_hash == parse_skill_file(entry).content_hash
    assert document.directory_path == str(entry.parent)


def test_parse_skill_file_rejects_empty_body(tmp_path: Path) -> None:
    entry = _write(tmp_path / "empty", "---\nname: X\ndescription: Y\n---\n\n   \n")
    with pytest.raises(SkillError, match="no instructions"):
        parse_skill_file(entry)


def test_parse_skill_file_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(SkillError, match="does not exist"):
        parse_skill_file(tmp_path / "nope" / "SKILL.md")


def test_parse_skill_file_rejects_non_utf8(tmp_path: Path) -> None:
    entry = tmp_path / "bad" / "SKILL.md"
    entry.parent.mkdir(parents=True)
    entry.write_bytes(b"---\nname: \xff\xfe\n---\nbody\n")
    with pytest.raises(SkillError, match="not valid UTF-8"):
        parse_skill_file(entry)


def test_parse_skill_file_rejects_oversized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.skills import loader

    monkeypatch.setattr(loader, "MAX_BODY_BYTES", 10)
    entry = _write(tmp_path / "big", VALID)
    with pytest.raises(SkillError, match="above the"):
        parse_skill_file(entry)


def test_load_skill_directory_uses_folder_name(tmp_path: Path) -> None:
    _write(tmp_path / "code-review", VALID)
    document = load_skill_directory(tmp_path / "code-review")
    assert document.slug == "code-review"


def test_discover_skills_reports_valid_and_broken(tmp_path: Path) -> None:
    _write(tmp_path / "good", VALID)
    _write(tmp_path / "broken", "no frontmatter here")
    results = {path.parent.name: error for path, error in discover_skills(tmp_path)}
    assert results["good"] is None
    assert isinstance(results["broken"], SkillError)


def test_discover_skills_does_not_follow_symlinked_directories(tmp_path: Path) -> None:
    outside = _write(tmp_path.parent / "outside-skill", VALID)
    root = tmp_path / "root"
    root.mkdir()
    (root / "linked").symlink_to(outside.parent, target_is_directory=True)
    # A directory symlink is not descended into, so nothing outside the root is read.
    assert list(discover_skills(root)) == []
    assert outside.is_file()


def test_discover_skills_missing_root_yields_nothing(tmp_path: Path) -> None:
    assert list(discover_skills(tmp_path / "absent")) == []


def test_copy_skill_directory_skips_git_and_pycache(tmp_path: Path) -> None:
    source = tmp_path / "src-skill"
    _write(source, VALID)
    (source / ".git").mkdir()
    (source / ".git" / "config").write_text("x")
    (source / "__pycache__").mkdir()
    (source / "__pycache__" / "a.pyc").write_text("x")
    (source / "helper.md").write_text("extra")

    target = copy_skill_directory(source, tmp_path / "root", "copy")
    assert (target / "SKILL.md").is_file()
    assert (target / "helper.md").is_file()
    assert not (target / ".git").exists()
    assert not (target / "__pycache__").exists()


def test_copy_skill_directory_replaces_existing(tmp_path: Path) -> None:
    source = tmp_path / "src-skill"
    _write(source, VALID)
    root = tmp_path / "root"
    stale = root / "copy"
    stale.mkdir(parents=True)
    (stale / "stale.txt").write_text("old")

    target = copy_skill_directory(source, root, "copy")
    assert not (target / "stale.txt").exists()
    assert (target / "SKILL.md").is_file()


def test_skill_prompt_contains_name_version_and_body(tmp_path: Path) -> None:
    document = load_skill_directory(Path(_write(tmp_path / "deploy-service", VALID).parent))
    rendered = skill_prompt(document)
    assert "# Skill: Deploy Service (v2.1.0)" in rendered
    assert "Deploy a service safely." in rendered
    assert "1. Check health." in rendered
