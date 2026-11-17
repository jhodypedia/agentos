"""SKILL.md discovery, parsing and validation.

A skill is a directory containing ``SKILL.md``: YAML frontmatter followed by a
Markdown body of instructions. The frontmatter is the metadata contract, the
body is what an agent reads when the skill is loaded. Nothing here talks to the
database — the loader is a pure function of the filesystem so it can be tested
and used by the CLI without a session.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SKILL_FILENAME = "SKILL.md"
MAX_NAME_LENGTH = 160
MAX_DESCRIPTION_LENGTH = 2000
MAX_BODY_BYTES = 256_000

_SLUG_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_SEMVER_PATTERN = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")


class SkillError(Exception):
    """A skill could not be parsed, validated or installed."""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


@dataclass(slots=True)
class SkillDocument:
    """A parsed SKILL.md, validated and ready to persist."""

    slug: str
    name: str
    description: str
    version: str
    body: str
    frontmatter: dict[str, Any] = field(default_factory=dict)
    entry_path: str = SKILL_FILENAME
    directory_path: str = ""
    content_hash: str = ""

    @property
    def allowed_tools(self) -> list[str]:
        """Tools the skill explicitly declares (informational, never a grant)."""
        raw = self.frontmatter.get("allowed-tools") or self.frontmatter.get("allowed_tools")
        if raw is None:
            return []
        if isinstance(raw, str):
            return [part.strip() for part in raw.replace(",", " ").split() if part.strip()]
        if isinstance(raw, list):
            return [str(item).strip() for item in raw if str(item).strip()]
        return []

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "frontmatter": self.frontmatter,
            "entry_path": self.entry_path,
            "directory_path": self.directory_path,
            "content_hash": self.content_hash,
            "bytes": len(self.body.encode("utf-8")),
        }


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split ``---`` delimited YAML frontmatter from the Markdown body.

    A file with no frontmatter is rejected: metadata is required, not optional,
    because the catalogue and on-demand loading both depend on it.
    """
    if not text.startswith("---"):
        raise SkillError("SKILL.md must start with a YAML frontmatter block")

    lines = text.splitlines()
    closing: int | None = None
    for index in range(1, len(lines)):
        if lines[index].strip() in {"---", "..."}:
            closing = index
            break
    if closing is None:
        raise SkillError("SKILL.md frontmatter block is not closed with '---'")

    raw_frontmatter = "\n".join(lines[1:closing])
    body = "\n".join(lines[closing + 1 :]).lstrip("\n")

    try:
        parsed = yaml.safe_load(raw_frontmatter) if raw_frontmatter.strip() else {}
    except yaml.YAMLError as exc:
        raise SkillError(f"SKILL.md frontmatter is not valid YAML: {exc}") from exc

    if parsed is None:
        parsed = {}
    if not isinstance(parsed, dict):
        raise SkillError("SKILL.md frontmatter must be a mapping of keys to values")
    return parsed, body


def _require_str(frontmatter: dict[str, Any], key: str, *, max_length: int, field_name: str) -> str:
    value = frontmatter.get(key)
    if value is None:
        raise SkillError(f"Missing required frontmatter field '{field_name}'")
    if not isinstance(value, str):
        raise SkillError(f"Frontmatter field '{field_name}' must be a string")
    stripped = value.strip()
    if not stripped:
        raise SkillError(f"Frontmatter field '{field_name}' must not be empty")
    if len(stripped) > max_length:
        raise SkillError(f"Frontmatter field '{field_name}' is longer than {max_length} characters")
    return stripped


def validate_frontmatter(
    frontmatter: dict[str, Any], *, default_slug: str | None = None
) -> dict[str, str]:
    """Validate metadata and return the normalised fields.

    The slug comes from the ``slug`` key, else the directory name, else the
    display name. An explicit ``slug`` is validated verbatim — silently
    repairing it would let two skills collide — while a slug derived from a
    name or folder is normalised first.
    """
    explicit = frontmatter.get("slug")
    if explicit is not None:
        if not isinstance(explicit, str) or not explicit.strip():
            raise SkillError("Frontmatter field 'slug' must be a non-empty string")
        slug = explicit.strip()
        if not _SLUG_PATTERN.match(slug):
            raise SkillError(
                f"Skill slug '{slug}' is invalid: use lowercase letters, digits and single hyphens"
            )
    else:
        derived = default_slug or frontmatter.get("name")
        if not isinstance(derived, str) or not derived.strip():
            raise SkillError("A skill needs a 'slug' (or a directory name usable as one)")
        slug = _normalise_slug(derived)
        if not _SLUG_PATTERN.match(slug):
            raise SkillError(
                f"Skill slug '{slug}' is invalid: use lowercase letters, digits and single hyphens"
            )

    name = _require_str(frontmatter, "name", max_length=MAX_NAME_LENGTH, field_name="name")
    description = _require_str(
        frontmatter,
        "description",
        max_length=MAX_DESCRIPTION_LENGTH,
        field_name="description",
    )

    raw_version = frontmatter.get("version", "1.0.0")
    if not isinstance(raw_version, str):
        raw_version = str(raw_version)
    version = raw_version.strip() or "1.0.0"
    if not _SEMVER_PATTERN.match(version):
        raise SkillError(f"Skill version '{version}' is not semver (expected e.g. 1.2.0)")

    return {"slug": slug, "name": name, "description": description, "version": version}


def _normalise_slug(raw: str) -> str:
    slug = raw.strip().lower().replace("_", "-").replace(" ", "-")
    slug = re.sub(r"[^a-z0-9-]", "", slug)
    return re.sub(r"-{2,}", "-", slug).strip("-")


def parse_skill_file(path: Path, *, default_slug: str | None = None) -> SkillDocument:
    """Read and validate one SKILL.md."""
    if not path.is_file():
        raise SkillError(f"{path} does not exist")
    size = path.stat().st_size
    if size > MAX_BODY_BYTES:
        raise SkillError(f"{path.name} is {size} bytes, above the {MAX_BODY_BYTES} byte limit")

    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise SkillError(f"{path.name} is not valid UTF-8") from exc

    frontmatter, body = split_frontmatter(text)
    fields = validate_frontmatter(frontmatter, default_slug=default_slug)
    if not body.strip():
        raise SkillError("SKILL.md has no instructions after the frontmatter")

    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return SkillDocument(
        slug=fields["slug"],
        name=fields["name"],
        description=fields["description"],
        version=fields["version"],
        body=body,
        frontmatter=frontmatter,
        entry_path=SKILL_FILENAME,
        directory_path=str(path.parent),
        content_hash=digest,
    )


def discover_skills(root: Path) -> Iterator[tuple[Path, SkillError | None]]:
    """Yield ``(skill_file, error)`` for every SKILL.md under ``root``.

    A broken skill must not stop the scan, so parse errors are yielded alongside
    the path and the caller decides whether to report or ignore them. Symlinks
    are not followed at all (``rglob`` does not descend into them), so a skill
    directory can never read outside the root.
    """
    if not root.exists():
        return
    for candidate in sorted(root.rglob(SKILL_FILENAME)):
        if candidate.is_symlink() or any(parent.is_symlink() for parent in candidate.parents):
            yield candidate, SkillError("Refusing to load a skill reached through a symlink")
            continue
        try:
            resolved = candidate.resolve()
            resolved.relative_to(root.resolve())
        except (OSError, ValueError):
            yield candidate, SkillError("Skill path escapes the skills root")
            continue
        try:
            parse_skill_file(candidate, default_slug=candidate.parent.name)
        except SkillError as exc:
            yield candidate, exc
        else:
            yield candidate, None


def load_skill_directory(directory: Path) -> SkillDocument:
    """Load a single skill directory (the directory name is the default slug)."""
    return parse_skill_file(directory / SKILL_FILENAME, default_slug=directory.name)


def copy_skill_directory(source: Path, destination_root: Path, slug: str) -> Path:
    """Copy a skill directory into the managed root under a sanitised name.

    The copy is deliberately shallow-but-complete (tree copy, no symlink
    following) so a skill can ship supporting files without escaping the root.
    """
    target = destination_root / slug
    if target.exists() and target.resolve() == source.resolve():
        # Installing a directory that already lives at its destination must not
        # delete the source it is about to copy from.
        return target
    if target.exists():
        shutil.rmtree(target)
    destination_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        source,
        target,
        symlinks=False,
        ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", "node_modules"),
    )
    return target


def skill_prompt(document: SkillDocument) -> str:
    """Render a loaded skill for a model context.

    The header is the catalogue entry the model saw; the body is the procedure.
    """
    return (
        f"# Skill: {document.name} (v{document.version})\n"
        f"{document.description}\n\n"
        f"{document.body.strip()}"
    )


def catalogue_entry(document: SkillDocument) -> dict[str, Any]:
    return {
        "slug": document.slug,
        "name": document.name,
        "description": document.description,
        "version": document.version,
    }


def skills_to_json(documents: list[SkillDocument]) -> str:
    return json.dumps([catalogue_entry(doc) for doc in documents], ensure_ascii=False)
