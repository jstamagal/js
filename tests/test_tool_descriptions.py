from __future__ import annotations

from pathlib import Path

import pytest

from js.toolkit import descriptions
from js.toolkit.registry import build_default_registry


CORE_TOOL_NAMES = {
    "read",
    "write",
    "fs_search",
    "sem_search",
    "remove",
    "patch",
    "multi_patch",
    "undo",
    "shell",
    "fetch",
    "followup",
    "plan",
    "skill",
    "todo_write",
    "todo_read",
    "task",
}


def _prompt_agent_names() -> set[str]:
    prompts_root = Path(__file__).resolve().parents[1] / "prompts"
    return {path.name for path in prompts_root.iterdir() if path.is_dir() and any(path.glob("*.md"))}


def test_description_loader_rejects_missing_and_empty_files(tmp_path, monkeypatch):
    monkeypatch.setattr(descriptions, "_DESCRIPTION_DIR", tmp_path)
    descriptions.load_description.cache_clear()

    with pytest.raises(FileNotFoundError):
        descriptions.load_description("missing")

    (tmp_path / "empty.md").write_text("\n", encoding="utf-8")
    with pytest.raises(ValueError):
        descriptions.load_description("empty")

    descriptions.load_description.cache_clear()


def test_registered_tools_and_description_files_match():
    descriptions.load_description.cache_clear()
    registry = build_default_registry()
    registered = {tool.name for tool in registry.tools}
    files = {path.stem for path in descriptions._DESCRIPTION_DIR.glob("*.md")}
    generated_agent_tools = _prompt_agent_names() - files

    assert CORE_TOOL_NAMES.issubset(registered)
    assert files == registered - generated_agent_tools
    for tool in registry.tools:
        assert tool.description.strip()
        if tool.name in files:
            assert descriptions.load_description(tool.name) == tool.description



def test_file_tool_rename_and_alias_resolution():
    registry = build_default_registry()

    assert {tool.name for tool in registry.tools if tool.name in CORE_TOOL_NAMES} == CORE_TOOL_NAMES
    assert registry.resolve("read").name == "read"
    assert registry.resolve("Read").name == "read"
    assert registry.resolve("write").name == "write"
    assert registry.resolve("Write").name == "write"
    assert registry.resolve("task").name == "task"
    assert registry.resolve("Task").name == "task"
    assert registry.resolve("fs_search").name == "fs_search"
    assert registry.resolve("sem_search").name == "sem_search"
    assert registry.resolve("remove").name == "remove"
    assert registry.resolve("patch").name == "patch"
    assert registry.resolve("multi_patch").name == "multi_patch"
    assert registry.resolve("undo").name == "undo"
    assert registry.resolve("fs_read") is None
    assert registry.resolve("cat") is None
    assert registry.resolve("grep") is None
    assert registry.resolve("forge__read_file") is None


def test_core_tool_schemas_match_canonical_surface_names():
    registry = build_default_registry()

    read = registry.resolve("read")
    write = registry.resolve("write")
    search = registry.resolve("fs_search")
    task = registry.resolve("task")

    assert read.required == ("file_path",)
    assert set(read.params) == {"file_path", "range", "show_line_numbers"}
    assert write.required == ("file_path", "content")
    assert set(write.params) == {"file_path", "content", "overwrite"}
    assert {"-A", "-B", "-C", "-i", "-n", "type"}.issubset(search.params)
    assert task.required == ("tasks", "agent_id")
    assert set(task.params) == {"tasks", "agent_id", "session_id", "model"}


def test_named_agent_tools_are_generated_from_prompt_dirs():
    registry = build_default_registry()
    # discover real agent dirs so adding/removing a prompt agent never snaps this test.
    prompt_agents = sorted(p.name for p in Path("prompts").iterdir() if p.is_dir())

    assert prompt_agents, "prompts/ should expose at least one agent dir"
    for name in prompt_agents:
        tool = registry.resolve(name)
        assert tool is not None, name
        assert tool.name == name
        assert tool.required == ("tasks",), name
        assert set(tool.params) == {"tasks"}, name

    assert [tool.name for tool in registry.select(prompt_agents).tools] == prompt_agents


def test_wiki_and_artifact_tool_params_have_descriptions():
    registry = build_default_registry()

    for tool in registry.tools:
        if not (tool.name.startswith("wiki_") or tool.name.startswith("artifact_")):
            continue
        for name, schema in tool.params.items():
            assert schema.get("description", "").strip(), f"{tool.name}.{name}"


# --- co-present-tool-name conditional blocks -------------------------------- #

R = descriptions.render_tool_name_sections


def test_unless_block_kept_only_when_none_of_its_tools_present():
    text = "base\n{{#unless fs_search}}rg doctrine{{/unless}}\ntail"
    assert R(text, {"shell"}) == "base\nrg doctrine\ntail"
    assert R(text, {"shell", "fs_search"}) == "base\ntail"


def test_if_block_kept_only_when_any_of_its_tools_present():
    text = "x\n{{#if fs_search}}use fs_search{{/if}}\ny"
    assert R(text, {"fs_search"}) == "x\nuse fs_search\ny"
    assert R(text, {"shell"}) == "x\ny"


def test_multiple_names_unless_emits_only_when_none_present():
    text = "{{#unless fs_search sem_search}}doctrine{{/unless}}"
    assert R(text, {"shell"}) == "doctrine"
    assert R(text, {"sem_search"}) == ""
    assert R(text, {"fs_search"}) == ""


def test_multiple_names_if_emits_when_any_present():
    text = "{{#if fs_search sem_search}}search doctrine{{/if}}"
    assert R(text, {"sem_search"}) == "search doctrine"
    assert R(text, set()) == ""


def test_nested_block_is_rejected_and_left_literal(capsys):
    descriptions._WARNED.clear()
    text = "{{#unless a}}outer {{#if b}}inner{{/if}}{{/unless}}"
    # Degrade to literal, never resolve a nested block.
    assert R(text, set()) == text
    assert "nested" in capsys.readouterr().err


def test_malformed_unbalanced_block_degrades_to_literal(capsys):
    descriptions._WARNED.clear()
    text = "{{#unless a}}no close tag here"
    assert R(text, set()) == text
    assert "unbalanced" in capsys.readouterr().err


def test_block_with_no_tool_name_degrades_to_literal(capsys):
    descriptions._WARNED.clear()
    text = "{{#if   }}body{{/if}}"  # opener present but names its zero tools
    assert R(text, set()) == text
    assert "names no tool" in capsys.readouterr().err


def test_mismatched_open_close_kinds_do_not_pair():
    # An {{#if}} must not close on {{/unless}}; the pair never matches, so the
    # text is emitted literally (and flagged as unbalanced).
    text = "{{#if a}}body{{/unless}}"
    assert R(text, {"a"}) == text


def test_backslash_escape_emits_block_verbatim_minus_one_backslash():
    text = r"\{{#unless fs_search}}doc{{/unless}}"
    assert R(text, {"shell"}) == "{{#unless fs_search}}doc{{/unless}}"


def test_backtick_span_keeps_block_literal():
    text = "`{{#if x}}doc{{/if}}`"
    assert R(text, {"x"}) == "`{{#if x}}doc{{/if}}`"


def test_kept_body_is_not_rescanned_for_directives():
    # Single-pass injection guard: a directive-looking string inside a kept body
    # is spliced in verbatim, not evaluated as another block.
    text = "{{#unless fs_search}}see {{#if evil}}x{{/if}} literally{{/unless}}"
    # The inner opener trips the nested guard -> whole outer block left literal.
    descriptions._WARNED.clear()
    assert R(text, {"shell"}) == text


def test_text_without_markers_is_returned_unchanged():
    text = "plain description, no conditionals"
    assert R(text, {"shell"}) is text


def test_registry_surface_composition_shell_only_vs_with_fs_search():
    """The headline: a shell-only agent is taught the rg/fd doctrine; an agent
    that also has fs_search is not schooled on rg/fd and is pointed at fs_search."""
    full = build_default_registry()

    def shell_desc(selectors):
        specs = full.select(selectors).openai_specs()
        return next(s["function"]["description"] for s in specs if s["function"]["name"] == "shell")

    shell_only = shell_desc(["shell"])
    assert "ripgrep" in shell_only or "`rg`" in shell_only
    assert "`fd`" in shell_only
    assert "use `fs_search`" not in shell_only

    with_search = shell_desc(["shell", "fs_search"])
    assert "use `fs_search`" in with_search
    assert "ripgrep" not in with_search
    assert "`fd`" not in with_search


def test_openai_specs_never_leak_raw_markers_on_any_surface():
    full = build_default_registry()
    surfaces = [
        ["shell"], ["read"], ["fs_search"], ["sem_search"], ["shell", "read"],
        ["read", "fs_search", "sem_search", "patch", "write", "task"], None,
    ]
    for sel in surfaces:
        registry = full if sel is None else full.select(sel)
        for spec in registry.openai_specs():
            desc = spec["function"]["description"]
            assert "{{#" not in desc and "{{/" not in desc, (sel, spec["function"]["name"])


def test_rendered_surfaces_never_mention_unavailable_core_tools():
    full = build_default_registry()
    surfaces = [
        ["shell"],
        ["shell", "read", "write", "patch", "multi_patch", "remove", "undo"],
        ["shell", "read", "write", "patch", "fs_search"],
        ["read"],
        ["write"],
        ["patch"],
        ["remove"],
        ["fs_search"],
        ["sem_search"],
        ["task"],
        ["read", "write", "fs_search", "patch", "undo", "shell"],  # commit agent
        [
            "read", "write", "fs_search", "sem_search", "remove", "patch",
            "multi_patch", "undo", "shell", "fetch", "todo_read", "todo_write",
            "followup", "plan", "skill", "task",
        ],
        None,
    ]
    core = CORE_TOOL_NAMES | {"multi_patch"}

    for sel in surfaces:
        registry = full if sel is None else full.select(sel)
        present = {tool.name for tool in registry.tools}
        for spec in registry.openai_specs():
            tool = spec["function"]["name"]
            desc = spec["function"]["description"]
            for name in core - present:
                assert f"`{name}`" not in desc, (sel, tool, name)


def test_shell_only_surface_gets_missing_tool_doctrine_without_phantom_tools():
    full = build_default_registry()
    shell_desc = next(
        spec["function"]["description"]
        for spec in full.select(["shell"]).openai_specs()
        if spec["function"]["name"] == "shell"
    )

    assert "Content search: use `rg`" in shell_desc
    assert "File finding: use `fd`" in shell_desc
    assert "Inspect known files with" in shell_desc
    assert "Create complete files with" in shell_desc
    assert "Edit existing files with" in shell_desc
    assert "Remove files with" in shell_desc
    assert "Download with" in shell_desc
    for absent in CORE_TOOL_NAMES - {"shell"}:
        assert f"`{absent}`" not in shell_desc
