from __future__ import annotations

from js.toolkit.core import compact_json
from js.toolkit.descriptions import render_tool_name_sections
from js.toolkit.registry import build_default_registry


def _row_bytes(registry, name: str, surface: list[str]) -> tuple[int, int]:
    """(rendered description bytes, schema bytes) for one tool on one surface."""
    tool = registry.resolve(name)
    assert tool is not None
    rendered = len(render_tool_name_sections(tool.description, surface, tool=name).encode("utf-8"))
    schema = len(compact_json(tool.openai_spec()["function"]["parameters"]).encode("utf-8"))
    return rendered, schema


def test_shell_row_shrinks_when_fs_search_is_absent():
    # The slim shell description swaps a two-line fs_search pointer for a one-line
    # rg/fd note when fs_search leaves the surface, so the row must shrink, and
    # the shrink is description-only — the schema never moves. (Stock swaps in a
    # longer rg/fd doctrine block, so only the swap itself is asserted there.)
    registry = build_default_registry(descriptions="slim")
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


def test_compare_table_reports_every_variant_and_the_saving():
    from js.settings import TOOL_DESCRIPTION_VARIANTS
    from js.tooldiag import render_compare

    registries = {
        variant: build_default_registry(descriptions=variant).select(["shell", "read"])
        for variant in TOOL_DESCRIPTION_VARIANTS
    }
    table = render_compare(registries, ["shell", "read"])
    header = table.splitlines()[1]
    for variant in TOOL_DESCRIPTION_VARIANTS:
        assert variant in header
    assert "saved" in header
    total = next(line for line in table.splitlines() if line.startswith("TOTAL "))
    stock_total, slim_total = int(total.split()[1]), int(total.split()[2])
    assert slim_total < stock_total
