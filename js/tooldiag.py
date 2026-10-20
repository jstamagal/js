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


from . import messages as msgs

from .toolkit.core import compact_json
from .toolkit.descriptions import render_tool_name_sections
from .toolkit.policy import ToolPolicyError
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
    # The model is charged for the rendered description and the schema; the raw
    # markdown is a diagnostic column and never counted in the total.
    rows.sort(key=lambda row: row[2] + row[3], reverse=True)

    lines = [
        f"surface: {', '.join(surface)} ({len(surface)} tools)",
        f"{'tool':<20}{'raw.md':>10}{'rendered':>10}{'schema':>10}{'total':>10}",
    ]
    for name, raw, rendered, schema in rows:
        lines.append(f"{name:<20}{raw:>10}{rendered:>10}{schema:>10}{rendered + schema:>10}")
    total_raw = sum(row[1] for row in rows)
    total_rendered = sum(row[2] for row in rows)
    total_schema = sum(row[3] for row in rows)
    lines.append(
        f"{'TOTAL':<20}{total_raw:>10}{total_rendered:>10}{total_schema:>10}"
        f"{total_rendered + total_schema:>10}"
    )
    return "\n".join(lines)


def _entry(name: str) -> str:
    """A `--surface` name as a policy entry: `shell` becomes `shell:eager`;
    `shell:lazy` (the noun:modifier form js uses everywhere) is kept as typed."""
    return name if ":" in name else f"{name}:eager"


def _registry(names: list[str]) -> ToolRegistry | None:
    registry = build_default_registry()
    if names:
        registry = registry.select([_entry(name) for name in names])
        if not registry.tools:
            return None
    return registry


def main(argv: list[str] | None = None) -> int:
    parser = msgs.ArgumentParser(prog="js.tooldiag", description=msgs.TOOLDIAG_DESCRIPTION.words())
    parser.add_argument(
        "--surface",
        default=None,
        help=msgs.OPT_TOOLDIAG_SURFACE.words(),
    )
    args = parser.parse_args(argv)
    names = parse_surface(args.surface or "")
    if args.surface is not None and not names:
        # `--surface ,` named nothing; the full table would answer another question.
        msgs.warn(msgs.TOOLDIAG_NO_TOOLS)
        return 1
    try:
        registry = _registry(names)
    except ToolPolicyError as exc:
        msgs.warn(msgs.FAILED, error=exc)
        return 2
    if registry is None:
        msgs.warn(msgs.TOOLDIAG_NO_TOOLS)
        return 1
    print(render_table(registry, [tool.name for tool in registry.tools]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
