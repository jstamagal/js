"""Positive checks for model-facing descriptions and JSON schemas."""

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


def test_fs_search_schema_exposes_filename_mode_and_both_flag_spellings():
    params = _specs("fs_search")["fs_search"]["parameters"]

    assert params["properties"]["output_mode"]["enum"] == [
        "files",
        "content",
        "files_with_matches",
        "count",
    ]
    assert {
        "-B", "before_context", "-A", "after_context", "-C", "context_lines",
        "-n", "show_line_numbers", "-i", "case_insensitive", "type", "file_type",
    }.issubset(params["properties"])


def test_patch_schema_has_complete_scalar_and_nonempty_batch_forms():
    schema = _specs("patch")["patch"]["parameters"]
    scalar, batch = schema["oneOf"]

    assert scalar["required"] == ["file_path", "old_string", "new_string"]
    assert scalar["additionalProperties"] is False
    assert batch["required"] == ["file_path", "edits"]
    assert batch["properties"]["edits"]["minItems"] == 1
    assert batch["properties"]["edits"]["items"]["required"] == [
        "old_string",
        "new_string",
    ]
    assert batch["additionalProperties"] is False


def test_todo_item_contract_requires_content_and_defaults_status(tmp_path):
    tool = build_default_registry().resolve("todo_write")
    item = tool.openai_spec()["function"]["parameters"]["properties"]["todos"]["items"]

    assert item["required"] == ["content"]
    assert item["properties"]["content"]["minLength"] == 1
    assert item["properties"]["content"]["pattern"] == r"\S"
    assert item["properties"]["status"]["default"] == "pending"
    result = call_tool(
        tool,
        {"todos": [{"content": "model contract"}]},
        ToolContext(cwd=tmp_path),
    )
    assert "('model contract', 'pending')" in result


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

    assert properties["browse"]["dump"]["enum"] == [
        "markdown", "text", "html", "links", "original", "assets", "cookies",
    ]
    assert properties["terminal_session"]["action"]["enum"] == [
        "start", "send", "look", "stop", "list",
    ]
    assert properties["wiki_write"]["kind"]["enum"] == [
        "source", "entity", "concept", "synthesis",
    ]
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


def test_rendered_file_and_terminal_descriptions_state_the_real_contracts():
    specs = _specs(
        "fs_search", "shell", "patch", "read", "todo_write",
        "terminal_session", "terminal_snapshot",
    )

    assert "In `files` mode, `pattern` is instead a filename/path glob" in specs["fs_search"]["description"]
    assert "Directory-only discovery: use `shell` with `fd --type d`" in specs["shell"]["description"]
    assert "The final filesystem write itself is not crash-atomic." in specs["patch"]["description"]
    assert "`.jsonl` records use the larger `ToolContext.jsonl_max_line_chars` limit." in specs["read"]["description"]
    assert "`status` is optional and defaults to `pending`" in specs["todo_write"]["description"]
    assert "they indicate passive change since the prior observation" in specs["terminal_session"]["description"]
    assert "`terminal_snapshot` also updates the comparison baseline" in specs["terminal_session"]["description"]


def test_every_mcp_control_explains_discovery_and_exact_server_names():
    host = MCPHost(MCPConfiguration((), MCPPolicy()))

    for entry in host.initial_catalog():
        assert "run tool_discovery with kind=\"mcp\"" in entry.description
        assert "pass the exact server name from a mcp:server:* status result" in entry.description
        assert entry.loadable is True
    for name in (
        "mcp_resource_read",
        "mcp_resource_subscribe",
        "mcp_resource_unsubscribe",
    ):
        description = dict(host.CONTROL_TOOLS)[name]
        assert "copy an exact returned URI" in description
