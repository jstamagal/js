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
    registry = build_default_registry()
    with_fs = [tool.name for tool in registry.tools]
    without_fs = [name for name in with_fs if name != "fs_search"]

    (rendered_with, schema_with) = _row_bytes(registry, "shell", with_fs)
    (rendered_without, schema_without) = _row_bytes(registry, "shell", without_fs)

    # The {{#if fs_search}} section is the only surface-dependent part of the
    # shell description, so the row must shrink when fs_search leaves the
    # surface, and the shrink is description-only — the schema never moves.
    assert rendered_without < rendered_with
    assert schema_with == schema_without

    shell = registry.resolve("shell")
    text_with = render_tool_name_sections(shell.description, with_fs, tool="shell")
    text_without = render_tool_name_sections(shell.description, without_fs, tool="shell")
    assert "fs_search" in text_with
    assert "fs_search" not in text_without
