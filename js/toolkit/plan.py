"""The plan tool: writes a versioned markdown plan under the plans directory."""

from __future__ import annotations

import os
from pathlib import Path

from .. import paths
from .core import Tool, ToolContext
from .descriptions import load_description, load_parameter_description
from .presentation import Presentation


def _filename_limit(directory: Path) -> int:
    """Longest single filename component this filesystem accepts, in bytes."""
    try:
        limit = os.pathconf(directory, "PC_NAME_MAX")
    except (OSError, ValueError):
        return 255
    return limit if limit > 0 else 255


def plan(
    plan_name: str,
    version: str,
    content: str,
    overwrite: bool = False,
    context: ToolContext | None = None,
) -> str:
    assert context is not None
    safe_name = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in plan_name).strip("-.") or "plan"
    safe_version = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in version).strip("-.") or "v1"
    filename = f"{safe_name}-{safe_version}.md"
    plans = paths.plans_dir()
    limit = _filename_limit(plans if plans.is_dir() else plans.parent if plans.parent.is_dir() else context.cwd)
    if len(filename.encode("utf-8")) > limit:
        return (
            f"ERROR: plan filename is {len(filename.encode('utf-8'))} bytes, over this "
            f"filesystem's {limit}-byte limit for one name; shorten plan_name or version"
        )
    target = plans / filename
    existed = target.exists()
    if existed and not overwrite:
        return f"ERROR: plan already exists at {target}; pass overwrite=true to replace it"
    context.snapshot(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    action = "overwritten" if existed else "written"
    return f"plan {action} at {target}" if existed else f"plan written to {target}"


def tools() -> tuple[Tool, ...]:
    return (
        Tool("plan", load_description("plan"), plan, {"plan_name": {"type": "string", "description": load_parameter_description('plan', 'plan_name')}, "version": {"type": "string", "description": load_parameter_description('plan', 'version')}, "content": {"type": "string", "description": load_parameter_description('plan', 'content')}, "overwrite": {"type": "boolean", "default": False, "description": load_parameter_description('plan', 'overwrite')}}, required=("plan_name", "version", "content"),
             presentation=Presentation(key=("plan_name",), body="content", lexer="markdown")),
    )
