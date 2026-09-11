"""tooldiag totals count only the columns the model is charged for."""
from __future__ import annotations

from js.tooldiag import render_table, surface_row
from js.toolkit.core import Tool, compact_json
from js.toolkit.descriptions import render_tool_name_sections
from js.toolkit.registry import ToolRegistry, build_default_registry


def _registry(*tools: Tool) -> ToolRegistry:
    return ToolRegistry(tuple(tools), {})


def _row_bytes(registry, name: str, surface: list[str]) -> tuple[int, int]:
    """(rendered description bytes, schema bytes) for one tool on one surface."""
    tool = registry.resolve(name)
    assert tool is not None
    rendered = len(render_tool_name_sections(tool.description, surface, tool=name).encode("utf-8"))
    schema = len(compact_json(tool.openai_spec()["function"]["parameters"]).encode("utf-8"))
    return rendered, schema


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


def test_shell_row_shrinks_when_fs_search_is_absent():
    # The shell description swaps a two-line fs_search pointer for a one-line
    # rg/fd note when fs_search leaves the surface, so the row must shrink, and
    # the shrink is description-only — the schema never moves.
    registry = build_default_registry()
    with_fs = [tool.name for tool in registry.tools]
    without_fs = [name for name in with_fs if name != "fs_search"]

    (rendered_with, schema_with) = _row_bytes(registry, "shell", with_fs)
    (rendered_without, schema_without) = _row_bytes(registry, "shell", without_fs)

    assert rendered_without < rendered_with
    assert schema_with == schema_without

    shell = registry.resolve("shell")
    text_with = render_tool_name_sections(shell.description, with_fs, tool="shell")
    text_without = render_tool_name_sections(shell.description, without_fs, tool="shell")
    assert "fs_search" in text_with
    assert "fs_search" not in text_without
