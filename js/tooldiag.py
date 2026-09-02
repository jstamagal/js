"""Diagnose how many bytes each tool costs the model.

One row per tool in a surface: the raw description markdown (build-time flag
blocks already resolved, as the Tool object carries it), the rendered
description that surface actually ships ({{#if}}/{{#unless}} co-present-tool
blocks resolved against the surface, the way ToolRegistry.openai_specs does),
the parameter schema as compact JSON, and the total of the two model-facing
columns. Totals are the surface's prompt cost; the raw column shows how much
each description carries before conditional sections resolve.

    uv run python -m js.tooldiag
    uv run python -m js.tooldiag --surface shell
    uv run python -m js.tooldiag --surface shell,read,write,undo
"""
from __future__ import annotations

import argparse
import sys

from .toolkit.core import compact_json
from .toolkit.descriptions import render_tool_name_sections
from .toolkit.registry import ToolRegistry, build_default_registry


def parse_surface(value: str) -> list[str]:
    names = [name.strip() for name in value.split(",")]
    return [name for name in names if name]


def surface_row(tool, surface: list[str]) -> tuple[int, int, int]:
    """(raw description bytes, rendered description bytes, schema bytes) for one tool on one surface."""
    raw = len(tool.description.encode("utf-8"))
    rendered = len(render_tool_name_sections(tool.description, surface, tool=tool.name).encode("utf-8"))
    schema = len(compact_json(tool.openai_spec()["function"]["parameters"]).encode("utf-8"))
    return raw, rendered, schema


def render_table(registry: ToolRegistry, surface: list[str]) -> str:
    rows = [(tool.name, *surface_row(tool, surface)) for tool in registry.tools]
    rows.sort(key=lambda row: row[1] + row[2] + row[3], reverse=True)

    lines = [
        f"surface: {', '.join(surface)} ({len(surface)} tools)",
        f"{'tool':<20}{'raw.md':>10}{'rendered':>10}{'schema':>10}{'total':>10}",
    ]
    for name, raw, rendered, schema in rows:
        lines.append(f"{name:<20}{raw:>10}{rendered:>10}{schema:>10}{raw + rendered + schema:>10}")
    lines.append(
        f"{'TOTAL':<20}"
        f"{sum(row[1] for row in rows):>10}"
        f"{sum(row[2] for row in rows):>10}"
        f"{sum(row[3] for row in rows):>10}"
        f"{sum(sum(row[1:]) for row in rows):>10}"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="js.tooldiag",
        description="Per-tool byte cost of model-facing descriptions and parameter schemas.",
    )
    parser.add_argument(
        "--surface",
        default="",
        help="comma-separated tool names; default is the full default registry",
    )
    args = parser.parse_args(argv)
    names = parse_surface(args.surface)
    registry = build_default_registry()
    if names:
        registry = registry.select(names)
        if not registry.tools:
            print("js: tooldiag: surface matched no tools", file=sys.stderr)
            return 1
    print(render_table(registry, [tool.name for tool in registry.tools]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
