from __future__ import annotations

import re
from pathlib import Path

import pytest

from js.toolkit import descriptions, fs
from js.toolkit.core import Tool
from js.toolkit.registry import ToolRegistry, build_default_registry


CORE_TOOL_NAMES = {
    "read",
    "write",
    "fs_search",
    "ast_search",
    "remove",
    "patch",
    "undo",
    "shell",
    "fetch",
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
    monkeypatch.setattr(descriptions, "_DESCRIPTIONS_ROOT", tmp_path)
    descriptions._load_description.cache_clear()

    with pytest.raises(FileNotFoundError):
        descriptions.load_description("missing")

    (tmp_path / "empty.md").write_text("\n", encoding="utf-8")
    with pytest.raises(ValueError):
        descriptions.load_description("empty")

    descriptions._load_description.cache_clear()


def test_registered_tools_and_description_files_match():
    descriptions._load_description.cache_clear()
    registry = build_default_registry()
    registered = {tool.name for tool in registry.tools}
    files = {path.stem for path in descriptions.description_dir().glob("*.md")}
    generated_agent_tools = _prompt_agent_names() - files
    turn_scoped_tools = {"tool_discovery"}

    assert CORE_TOOL_NAMES.issubset(registered)
    assert files - turn_scoped_tools == registered - generated_agent_tools
    for tool in registry.tools:
        assert tool.description.strip()
        if tool.name in files:
            assert descriptions.load_description(tool.name) == tool.description


def test_fetch_description_discloses_inline_ceiling_and_request_deadline():
    description = descriptions.load_description("fetch")

    assert "32 MiB inline-read ceiling" in description
    assert "use `save`" in description
    assert "whole unsaved request" in description


def test_browse_description_discloses_settle_window_and_original_decoding():
    description = " ".join(descriptions.load_description("browse").split())

    assert "five-second post-load settle window" in description
    assert "changes scheduled later than that may be absent" in description
    assert "decoded as UTF-8 text" in description
    assert "`fetch(save=...)` when exact bytes matter" in description


def test_ast_search_description_says_the_walk_crosses_mount_points():
    description = " ".join(descriptions.load_description("ast_search").split())

    assert "mount" in description
    assert "--one-file-system" in description


def test_ast_search_description_discloses_the_c_bare_call_pattern_limit():
    description = " ".join(descriptions.load_description("ast_search").split())

    assert "call($A);" in description
    assert "Cpp" in description



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
    assert registry.resolve("ast_search").name == "ast_search"
    assert registry.resolve("remove").name == "remove"
    assert registry.resolve("patch").name == "patch"
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
    ast_search = registry.resolve("ast_search")
    plan = registry.resolve("plan")
    task = registry.resolve("task")

    assert read.required == ("file_path",)
    assert set(read.params) == {"file_path", "range", "show_line_numbers"}
    assert write.required == ("file_path", "content")
    assert set(write.params) == {"file_path", "content", "overwrite"}
    assert {
        "before_context", "after_context", "context_lines",
        "case_insensitive", "show_line_numbers", "file_type",
    }.issubset(search.params)
    # rg-style spellings still reach the handler through **rg_flags but are not
    # declared: the schema is what the model reads, and declaring both doubled it.
    assert not {"-A", "-B", "-C", "-i", "-n", "type"} & set(search.params)
    assert ast_search.required == ("pattern",)
    assert set(ast_search.params) == {"pattern", "path", "lang", "rewrite", "apply", "max_results"}
    assert set(ast_search.params["lang"]["enum"]) == set(fs._AST_GREP_LANGUAGES)
    assert plan.required == ("plan_name", "version", "content")
    assert set(plan.params) == {"plan_name", "version", "content", "overwrite"}
    assert task.required == ("tasks", "agent_id")
    assert set(task.params) == {"tasks", "agent_id", "session_id", "model"}


def test_tools_reference_fs_search_section_matches_the_published_schema():
    page = (Path(__file__).resolve().parents[1] / "docs" / "tools-reference.md").read_text(encoding="utf-8")
    section = page.split("### `fs_search`", 1)[1].split("\n### ", 1)[0]
    documented = set(re.findall(r"^- `([a-z_]+)`", section, flags=re.MULTILINE))
    search = build_default_registry().resolve("fs_search")

    assert documented == set(search.params)
    modes = set(
        search.openai_spec()["function"]["parameters"]["properties"]["output_mode"]["enum"]
    )
    assert modes
    assert modes.issubset(set(re.findall(r"`([a-z_]+)`", section)))


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


def test_wiki_tool_params_have_descriptions():
    registry = build_default_registry()

    for tool in registry.tools:
        if not tool.name.startswith("wiki_"):
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
    text = "{{#unless fs_search read}}doctrine{{/unless}}"
    assert R(text, {"shell"}) == "doctrine"
    assert R(text, {"read"}) == ""
    assert R(text, {"fs_search"}) == ""


def test_multiple_names_if_emits_when_any_present():
    text = "{{#if fs_search read}}search doctrine{{/if}}"
    assert R(text, {"read"}) == "search doctrine"
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
    assert R(text, {"shell"}) == text


def test_registry_renders_conditionals_for_selected_surface():
    registry = ToolRegistry(
        tools=(
            Tool("subject", "{{#if helper}}present{{/if}}{{#unless helper}}absent{{/unless}}", lambda: "", {}),
            Tool("helper", "helper", lambda: "", {}),
        ),
        aliases={"subject": "subject", "helper": "helper"},
    )
    for selectors, expected in [(["subject"], "absent"), (["subject", "helper"], "present")]:
        specs = registry.select(selectors).openai_specs()
        assert next(spec["function"]["description"] for spec in specs
                    if spec["function"]["name"] == "subject") == expected


def test_openai_specs_never_leak_raw_markers_on_any_surface():
    full = build_default_registry()
    surfaces = [
        ["shell"], ["read"], ["fs_search"], ["shell", "read"],
        ["read", "fs_search", "patch", "write", "task"], None,
    ]
    for sel in surfaces:
        registry = full if sel is None else full.select(sel)
        for spec in registry.openai_specs():
            desc = spec["function"]["description"]
            assert "{{#" not in desc and "{{/" not in desc, (sel, spec["function"]["name"])
