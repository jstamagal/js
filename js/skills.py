"""Discover compact skill metadata and load instructions on demand."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

import yaml

from . import paths

_NAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,78}[A-Za-z0-9])?$")
_TOOL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:-]*$")
_MAX_DESCRIPTION = 500
_MAX_METADATA_BYTES = 64 * 1024


@dataclass(frozen=True)
class SkillMetadata:
    """The bounded, instruction-free portion of a skill."""

    name: str
    description: str
    tools: tuple[str, ...]
    source: str
    path: Path


@dataclass(frozen=True)
class _SkillRecord:
    metadata: SkillMetadata


class SkillCatalog:
    """A deterministic metadata index whose instruction bodies stay on disk."""

    def __init__(self, records: Iterable[_SkillRecord] = ()) -> None:
        ordered = sorted(records, key=lambda record: record.metadata.name.casefold())
        self._records = {record.metadata.name.casefold(): record for record in ordered}

    @classmethod
    def discover(
        cls,
        project_dir: Path,
        *,
        package_dir: Path | None = None,
        global_dir: Path | None = None,
    ) -> SkillCatalog:
        return discover_skills(
            project_dir, package_dir=package_dir, global_dir=global_dir
        )

    @property
    def skills(self) -> tuple[SkillMetadata, ...]:
        return tuple(record.metadata for record in self._records.values())

    def get(self, name: str) -> SkillMetadata | None:
        record = self._records.get(name.casefold())
        return record.metadata if record is not None else None

    lookup = get

    def search(self, query: str = "") -> tuple[SkillMetadata, ...]:
        terms = tuple(term.casefold() for term in query.split() if term)
        matches = []
        for record in self._records.values():
            metadata = record.metadata
            haystack = " ".join(
                (metadata.name, metadata.description, *metadata.tools, metadata.source)
            ).casefold()
            if all(term in haystack for term in terms):
                matches.append(metadata)
        return tuple(
            sorted(
                matches,
                key=lambda item: (
                    0 if terms and item.name.casefold() == " ".join(terms) else 1,
                    item.name.casefold(),
                    str(item.path),
                ),
            )
        )

    def load(self, name: str) -> str | None:
        record = self._records.get(name.casefold())
        if record is None:
            return None
        text = record.metadata.path.read_text(encoding="utf-8", errors="replace")
        _, body, _ = _split_frontmatter(record.metadata.path, text)
        return body


def discover_skills(
    project_dir: Path,
    *,
    package_dir: Path | None = None,
    global_dir: Path | None = None,
) -> SkillCatalog:
    """Index package, global, and project skills without retaining their bodies."""

    package_root = package_dir or Path(__file__).resolve().parent / "skills"
    global_root = global_dir or paths.global_skills_dir()
    layers = (
        ("package", (package_root,)),
        ("global", (global_root,)),
        # Keep the old local ordering: skills/ wins over .skills/.
        ("project", (project_dir / ".skills", project_dir / "skills")),
    )
    selected: dict[str, _SkillRecord] = {}
    for source, roots in layers:
        for root in roots:
            root_records: dict[str, _SkillRecord] = {}
            for path in _skill_paths(root):
                record = _index_skill(path, source)
                key = record.metadata.name.casefold()
                prior = root_records.get(key)
                if prior is not None:
                    raise ValueError(
                        f"duplicate skill name {record.metadata.name!r} in {root}: "
                        f"{prior.metadata.path} and {path}"
                    )
                root_records[key] = record
            selected.update(root_records)
    return SkillCatalog(selected.values())


def _skill_paths(root: Path) -> tuple[Path, ...]:
    if not root.is_dir():
        return ()
    candidates: dict[str, Path] = {}
    # Later layouts only fill names not already supplied by the older lookup form.
    for path in sorted(root.glob("*.md"), key=lambda item: item.name.casefold()):
        candidates.setdefault(path.stem.casefold(), path)
    for filename in ("README.md", "SKILL.md"):
        for path in sorted(root.glob(f"*/{filename}"), key=lambda item: str(item).casefold()):
            candidates.setdefault(path.parent.name.casefold(), path)
    return tuple(candidates.values())


def _index_skill(path: Path, source: str) -> _SkillRecord:
    with path.open("rb") as stream:
        raw = stream.read(_MAX_METADATA_BYTES)
    text = raw.decode("utf-8", errors="replace")
    manifest, body, _ = _split_frontmatter(path, text)
    derived_name = (
        path.stem
        if path.parent.name == "skills" and path.suffix == ".md"
        else path.parent.name
    )
    # A dot-directory can contain flat files too; only layout filenames derive from the parent.
    if path.name not in {"README.md", "SKILL.md"}:
        derived_name = path.stem

    name = _string_field(path, manifest, "name") or derived_name
    _validate_name(path, name)
    description = _string_field(path, manifest, "description")
    if not description:
        description = _derive_description(body, name)
    description = " ".join(description.split())[:_MAX_DESCRIPTION]
    tools = _tools_field(path, manifest)
    metadata = SkillMetadata(
        name=name, description=description, tools=tools, source=source, path=path
    )
    return _SkillRecord(metadata=metadata)


def _split_frontmatter(path: Path, text: str) -> tuple[dict[str, Any], str, int]:
    if not text.startswith("---") or text[:4] not in {"---\n", "---\r"}:
        return {}, text, 0
    match = re.search(r"\r?\n---[ \t]*(?:\r?\n|$)", text[3:])
    if match is None:
        raise ValueError(f"frontmatter in {path} is missing a closing ---")
    start = 3 + match.start()
    end = 3 + match.end()
    yaml_text = text[text.find("\n") + 1:start]
    try:
        manifest = yaml.safe_load(yaml_text) if yaml_text.strip() else {}
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML frontmatter in {path}: {exc}") from exc
    if manifest is None:
        manifest = {}
    if not isinstance(manifest, dict):
        raise ValueError(f"frontmatter in {path} must be a mapping")
    return manifest, text[end:], end


def _string_field(path: Path, manifest: dict[str, Any], field: str) -> str:
    value = manifest.get(field)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{field} frontmatter in {path} must be a string")
    return value.strip()


def _tools_field(path: Path, manifest: dict[str, Any]) -> tuple[str, ...]:
    value = manifest.get("tools")
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"tools frontmatter in {path} must be a list of strings")
    tools: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not _TOOL_RE.fullmatch(item.strip()):
            raise ValueError(f"tools frontmatter in {path} must contain safe non-empty strings")
        tool = item.strip()
        if tool in seen:
            raise ValueError(f"tools frontmatter in {path} contains duplicate {tool!r}")
        seen.add(tool)
        tools.append(tool)
    return tuple(tools)


def _validate_name(path: Path, name: str) -> None:
    if not _NAME_RE.fullmatch(name) or ".." in name:
        raise ValueError(f"unsafe skill name {name!r} in {path}")


def _derive_description(body: str, name: str) -> str:
    heading = ""
    paragraphs: list[str] = []
    current: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not heading and stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
            continue
        if not stripped:
            if current:
                paragraphs.append(" ".join(current))
                break
            continue
        if not stripped.startswith(("#", "```")):
            current.append(stripped)
    if current and not paragraphs:
        paragraphs.append(" ".join(current))
    if paragraphs:
        return paragraphs[0]
    return heading or name.replace("-", " ").replace("_", " ")
