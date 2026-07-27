"""Deterministic discovery and loading for turn-scoped tool surfaces."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ..paths import global_skills_dir
from .core import Tool, ToolContext
from .descriptions import load_description


DISCOVERY_TOOL_NAME = "tool_discovery"


@dataclass(frozen=True)
class SkillDefinition:
    """Compact skill metadata plus the instructions returned when loaded."""

    id: str
    name: str
    description: str
    source: str
    instructions: str
    tools: tuple[str, ...] = ()


def _frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text
    marker = text.find("\n---\n", 4)
    if marker < 0:
        return {}, text
    try:
        metadata = yaml.safe_load(text[4:marker]) or {}
    except yaml.YAMLError:
        return {}, text
    if not isinstance(metadata, dict):
        return {}, text
    return metadata, text[marker + 5 :]


def _skill_from_file(path: Path, *, source: str, name: str) -> SkillDefinition | None:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    metadata, instructions = _frontmatter(raw)
    description = str(metadata.get("description") or "").strip()
    if not description:
        description = next(
            (line.lstrip("# ").strip() for line in instructions.splitlines() if line.strip()),
            f"Load the {name} skill.",
        )
    declared = metadata.get("tools", ())
    if isinstance(declared, str):
        declared = (declared,)
    if not isinstance(declared, (list, tuple)):
        declared = ()
    tools = tuple(str(item).strip() for item in declared if str(item).strip())
    return SkillDefinition(
        id=f"skill:{name}",
        name=name,
        description=description,
        source=source,
        instructions=instructions.strip(),
        tools=tools,
    )


def discover_skills(cwd: Path) -> tuple[SkillDefinition, ...]:
    """Find skills with deterministic project-over-global precedence."""

    found: dict[str, SkillDefinition] = {}
    roots = (
        (global_skills_dir(), "global"),
        (cwd / ".skills", "project"),
        (cwd / "skills", "project"),
    )
    for root, source in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*.md")):
            skill = _skill_from_file(path, source=source, name=path.stem)
            if skill is not None:
                found[skill.name] = skill
        for path in sorted(root.glob("*/SKILL.md")) + sorted(root.glob("*/README.md")):
            skill = _skill_from_file(path, source=source, name=path.parent.name)
            if skill is not None:
                found[skill.name] = skill
    return tuple(found[name] for name in sorted(found))


def discovery_tool(surface: Any) -> Tool:
    """Build the eager discovery tool bound to one turn's lazy surface."""

    def discover(
        query: str = "",
        kind: str = "",
        source: str = "",
        load: str = "",
        context: ToolContext | None = None,
    ) -> str:
        return surface.discover(query=query, kind=kind, source=source, load=load)

    return Tool(
        DISCOVERY_TOOL_NAME,
        load_description(DISCOVERY_TOOL_NAME),
        discover,
        {
            "query": {"type": "string", "description": "Words to find in catalog names and descriptions."},
            "kind": {"type": "string", "enum": ["native", "skill"], "description": "Optional catalog kind filter."},
            "source": {"type": "string", "description": "Optional exact source filter."},
            "load": {"type": "string", "description": "Stable catalog id to load, such as native:browser_open or skill:review."},
        },
    )


def compact_result(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
