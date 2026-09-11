"""tooldiag totals count only the columns the model is charged for."""
from __future__ import annotations

from js.tooldiag import render_table, surface_row
from js.toolkit.core import Tool
from js.toolkit.registry import ToolRegistry


def _registry(*tools: Tool) -> ToolRegistry:
    return ToolRegistry(tuple(tools), {})


def test_totals_are_the_rendered_description_plus_the_schema():
    tool = Tool("sample", "core\n{{#if shell}}shell-only detail{{/if}}", lambda: "", {})
    registry = _registry(tool)

    raw, rendered, schema = surface_row(tool, ["sample"])
    # The conditional section is absent on this surface, so raw and rendered differ.
    assert rendered != raw

    table = render_table(registry, ["sample"]).splitlines()

    assert table[2].split() == [
        "sample", str(raw), str(rendered), str(schema), str(rendered + schema),
    ]
    assert table[3].split() == [
        "TOTAL", str(raw), str(rendered), str(schema), str(rendered + schema),
    ]


def test_ranking_ignores_the_raw_markdown_column():
    heavy_raw = Tool("heavy", "core\n{{#if absent}}" + "x" * 1000 + "{{/if}}", lambda: "", {})
    plain = Tool("plain", "y" * 200, lambda: "", {})
    registry = _registry(heavy_raw, plain)

    table = render_table(registry, ["heavy", "plain"]).splitlines()

    assert table[2].split()[0] == "plain"
