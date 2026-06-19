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


def test_tool_descriptions_do_not_contain_porting_cruft():
    registry = build_default_registry()

    for tool in registry.tools:
        assert "forge" not in tool.description.lower(), tool.name


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


def test_core_tool_schemas_match_forge_surface_names():
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
