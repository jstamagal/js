"""Model-facing schemas agree with callable tools and handler behavior."""

from __future__ import annotations

from js.mcp.host import MCPHost
from js.mcp_config import MCPConfiguration, MCPPolicy
from js.toolkit.core import ToolContext, call_tool
from js.toolkit.registry import build_default_registry


def _specs(*names: str) -> dict[str, dict]:
    registry = build_default_registry().select(names)
    return {
        spec["function"]["name"]: spec["function"]
        for spec in registry.openai_specs()
    }


def test_fs_search_schema_exposes_filename_mode_and_readable_flag_names_only():
    params = _specs("fs_search")["fs_search"]["parameters"]

    assert set(params["properties"]["output_mode"]["enum"]) == {
        "files",
        "content",
        "files_with_matches",
        "count",
    }
    assert {
        "before_context", "after_context", "context_lines",
        "show_line_numbers", "case_insensitive", "file_type",
    }.issubset(params["properties"])
    # The handler still takes ripgrep's short spellings through **rg_flags, but
    # the schema is what the model reads and declaring both doubled it.
    assert not {"-B", "-A", "-C", "-n", "-i", "type"} & set(params["properties"])


def test_patch_schema_has_complete_scalar_and_nonempty_batch_forms():
    schema = _specs("patch")["patch"]["parameters"]
    props = schema["properties"]

    # Flat, not oneOf: llama.cpp's schema-to-grammar path emits {} for a bare
    # oneOf with no top-level properties, so a grammar-constrained model cannot
    # call patch at all. _apply_edit rejects mixing the two forms in code.
    assert "oneOf" not in schema
    assert schema["required"] == ["file_path"]
    assert schema["additionalProperties"] is False

    assert {"old_string", "new_string", "replace_all"}.issubset(props)
    assert props["edits"]["minItems"] == 1
    # Required names may be reordered, but duplicates make the schema invalid.
    assert sorted(props["edits"]["items"]["required"]) == ["new_string", "old_string"]
    assert props["edits"]["items"]["additionalProperties"] is False


def test_todo_item_contract_requires_content_and_defaults_status(tmp_path):
    tool = build_default_registry().resolve("todo_write")
    item = tool.openai_spec()["function"]["parameters"]["properties"]["todos"]["items"]

    assert item["required"] == ["content"]
    assert item["properties"]["content"]["minLength"] == 1
    assert "pattern" not in item["properties"]["content"]  # llama.cpp grammar path chokes on regex patterns; todo_write validates in code
    assert item["properties"]["status"]["default"] == "pending"
    context = ToolContext(cwd=tmp_path)
    call_tool(
        tool,
        {"todos": [{"content": "model contract"}]},
        context,
    )
    assert context.todos["model contract"].status == "pending"


def test_closed_sets_and_numeric_bounds_match_handler_contracts():
    specs = _specs(
        "serper_search",
        "tavily_search",
        "exa_search",
        "docs_search",
        "browse",
        "terminal_session",
        "terminal_snapshot",
        "browser_probe",
        "wiki_write",
    )
    properties = {
        name: spec["parameters"]["properties"]
        for name, spec in specs.items()
    }

    assert set(properties["browse"]["dump"]["enum"]) == {
        "markdown", "text", "html", "links", "original", "assets", "cookies",
    }
    assert set(properties["terminal_session"]["action"]["enum"]) == {
        "start", "send", "look", "stop", "list",
    }
    assert set(properties["wiki_write"]["kind"]["enum"]) == {
        "source", "entity", "concept", "synthesis",
    }
    assert properties["wiki_write"]["vault"]["minLength"] == 1
    assert (properties["serper_search"]["num"]["minimum"], properties["serper_search"]["num"]["maximum"]) == (1, 100)
    assert (properties["tavily_search"]["max_results"]["minimum"], properties["tavily_search"]["max_results"]["maximum"]) == (1, 20)
    assert (properties["exa_search"]["num"]["minimum"], properties["exa_search"]["num"]["maximum"]) == (1, 100)
    assert properties["exa_search"]["text_chars"]["minimum"] == 100
    assert properties["docs_search"]["tokens"]["minimum"] == 500
    assert (properties["terminal_session"]["wait_ms"]["minimum"], properties["terminal_session"]["wait_ms"]["maximum"]) == (0, 10_000)
    assert (properties["terminal_session"]["cols"]["minimum"], properties["terminal_session"]["cols"]["maximum"]) == (1, 400)
    assert (properties["terminal_session"]["rows"]["minimum"], properties["terminal_session"]["rows"]["maximum"]) == (1, 200)
    assert (properties["terminal_snapshot"]["wait_ms"]["minimum"], properties["terminal_snapshot"]["wait_ms"]["maximum"]) == (0, 10_000)
    assert (properties["browser_probe"]["settle_ms"]["minimum"], properties["browser_probe"]["settle_ms"]["maximum"]) == (0, 30_000)
    assert (properties["browser_probe"]["hold_ms"]["minimum"], properties["browser_probe"]["hold_ms"]["maximum"]) == (0, 30_000)
    assert (properties["browser_probe"]["viewport_width"]["minimum"], properties["browser_probe"]["viewport_width"]["maximum"]) == (100, 3840)
    assert (properties["browser_probe"]["viewport_height"]["minimum"], properties["browser_probe"]["viewport_height"]["maximum"]) == (100, 2160)


def test_mcp_control_catalog_ids_load_callable_tools():
    host = MCPHost(MCPConfiguration((), MCPPolicy()))
    entries = host.initial_catalog()
    assert {entry.name for entry in entries} == set(dict(host.CONTROL_TOOLS))
    for entry in entries:
        assert entry.loadable is True
        assert entry.description.strip()
        loaded = host.load(entry.id)
        assert loaded == [entry.name]
        tools = host.tools(loaded)
        assert [tool.openai_spec()["function"]["name"] for tool in tools] == [entry.name]
