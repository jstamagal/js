"""The skill tool: loads a model-invocable skill's instructions by name."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace

from ..skills import SkillMetadata, catalog_block, discover_skills, load_skill
from ..text_bytes import byte_size
from .core import Tool, ToolContext
from .descriptions import load_description, load_parameter_description
from .presentation import Presentation

_CATALOG_BYTES_MAX = 8_192


def skill(name: str, context: ToolContext | None = None, *, user_command: str = "/skill") -> str:
    assert context is not None
    catalog = context.skill_catalog
    if catalog is None:
        catalog = discover_skills(context.cwd, disabled=context.skills_disabled)
    metadata = catalog.get(name)
    if metadata is not None and not metadata.model_invocable:
        return f"ERROR: skill {metadata.name!r} is user-only; the user loads it with {user_command} {metadata.name}"
    try:
        loaded = load_skill(catalog, name, tool_registry=context.tool_registry)
    except (OSError, ValueError):
        return f"ERROR: skill {name!r} instructions could not be read"
    if loaded is not None:
        return loaded.render(getattr(context.tool_registry, "public_name", None))
    return f"ERROR: skill {name!r} not found in js/skills or local skills directories"


def with_catalog(tool: Tool, skills: Sequence[SkillMetadata]) -> Tool | None:
    """Advertise available skills within the schema budget, using discovery
    for larger catalogs. Return None when no skill is available."""
    if not skills:
        return None
    names = [skill.name for skill in skills]
    enum_bytes = byte_size(json.dumps(names))
    catalog = catalog_block(skills)
    name = dict(tool.params["name"])
    if enum_bytes <= _CATALOG_BYTES_MAX:
        name["enum"] = names
    if enum_bytes + byte_size(catalog) <= _CATALOG_BYTES_MAX:
        listing = catalog
    else:
        listing = 'Search available skills with tool_discovery and kind="skill".'
    description = tool.description.rstrip("\n") + "\n\n" + listing
    return replace(tool, description=description, params={**tool.params, "name": name})


def tools() -> tuple[Tool, ...]:
    return (
        Tool("skill", load_description("skill"), skill, {"name": {"type": "string", "description": load_parameter_description('skill', 'name')}}, required=("name",), read_only=True,
             presentation=Presentation(key=("name",))),
    )
