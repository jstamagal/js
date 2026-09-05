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
    uv run python -m js.tooldiag --descriptions slim
    uv run python -m js.tooldiag --compare --surface shell,read,write,undo

`--descriptions` picks the tool_descriptions/<variant> set (default: the active
knob, i.e. JS_TOOL_DESCRIPTIONS or stock). `--compare` renders every variant
against the same surface and reports the description bytes side by side; the
schema is the same in every variant, so it is shown once.
"""
from __future__ import annotations

import argparse
import sys

from .settings import TOOL_DESCRIPTION_VARIANTS
from .toolkit.core import compact_json
from .toolkit.descriptions import active_variant, render_tool_name_sections
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


def render_table(registry: ToolRegistry, surface: list[str], *, variant: str = "") -> str:
    rows = [(tool.name, *surface_row(tool, surface)) for tool in registry.tools]
    rows.sort(key=lambda row: row[1] + row[2] + row[3], reverse=True)

    label = f" descriptions: {variant}" if variant else ""
    lines = [
        f"surface: {', '.join(surface)} ({len(surface)} tools){label}",
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


def render_compare(registries: dict[str, ToolRegistry], surface: list[str]) -> str:
    """Rendered description bytes per variant for one surface, plus the shared
    schema column and the bytes the last variant saves over the first."""
    variants = list(registries)
    first, last = variants[0], variants[-1]
    names = [tool.name for tool in registries[first].tools]
    rendered = {
        variant: {
            tool.name: surface_row(tool, surface)[1] for tool in registry.tools
        }
        for variant, registry in registries.items()
    }
    schema = {tool.name: surface_row(tool, surface)[2] for tool in registries[first].tools}
    names.sort(key=lambda name: rendered[first][name] + schema[name], reverse=True)

    header = f"{'tool':<20}" + "".join(f"{variant:>10}" for variant in variants) + f"{'schema':>10}{'saved':>10}{'':>7}"
    lines = [f"surface: {', '.join(surface)} ({len(surface)} tools) rendered description bytes", header]

    def _pct(saved: int, base: int) -> str:
        return f"{(100 * saved / base):>6.0f}%" if base else f"{'':>7}"

    for name in names:
        saved = rendered[first][name] - rendered[last][name]
        lines.append(
            f"{name:<20}"
            + "".join(f"{rendered[variant][name]:>10}" for variant in variants)
            + f"{schema[name]:>10}{saved:>10}{_pct(saved, rendered[first][name])}"
        )
    totals = {variant: sum(rendered[variant].values()) for variant in variants}
    saved_total = totals[first] - totals[last]
    lines.append(
        f"{'TOTAL':<20}"
        + "".join(f"{totals[variant]:>10}" for variant in variants)
        + f"{sum(schema.values()):>10}{saved_total:>10}{_pct(saved_total, totals[first])}"
    )
    lines.append(
        f"{'TOTAL + schema':<20}"
        + "".join(f"{totals[variant] + sum(schema.values()):>10}" for variant in variants)
    )
    return "\n".join(lines)


def _registry(names: list[str], variant: str) -> ToolRegistry | None:
    registry = build_default_registry(descriptions=variant)
    if names:
        registry = registry.select(names)
        if not registry.tools:
            return None
    return registry


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
    parser.add_argument(
        "--descriptions",
        choices=TOOL_DESCRIPTION_VARIANTS,
        default=None,
        help="which tool_descriptions/<variant> set to measure; default is the active knob",
    )
    parser.add_argument(
        "--compare",
        action="store_true",
        help="render every variant against the surface and show description bytes side by side",
    )
    args = parser.parse_args(argv)
    names = parse_surface(args.surface)

    if args.compare:
        registries = {variant: _registry(names, variant) for variant in TOOL_DESCRIPTION_VARIANTS}
        if any(registry is None for registry in registries.values()):
            print("js: tooldiag: surface matched no tools", file=sys.stderr)
            return 1
        surface = [tool.name for tool in next(iter(registries.values())).tools]
        print(render_compare(registries, surface))
        return 0

    variant = args.descriptions or active_variant()
    registry = _registry(names, variant)
    if registry is None:
        print("js: tooldiag: surface matched no tools", file=sys.stderr)
        return 1
    print(render_table(registry, [tool.name for tool in registry.tools], variant=variant))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
