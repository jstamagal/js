"""The skill tool: loads a model-invocable skill's instructions by name."""

from __future__ import annotations

from ..skills import discover_skills, load_skill
from .core import Tool, ToolContext
from .descriptions import load_description, load_parameter_description
from .presentation import Presentation


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
        return loaded.render()
    return f"ERROR: skill {name!r} not found in js/skills or local skills directories"


def tools() -> tuple[Tool, ...]:
    return (
        Tool("skill", load_description("skill"), skill, {"name": {"type": "string", "description": load_parameter_description('skill', 'name')}}, required=("name",), read_only=True,
             presentation=Presentation(key=("name",))),
    )
