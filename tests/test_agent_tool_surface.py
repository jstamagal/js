from __future__ import annotations

from pathlib import Path

import pytest

from js import persona
from js import runtime
from js.config import Config
from js.model_client import ModelStreamResult
from js.toolkit import ToolContext
from js.toolkit.registry import build_default_registry, select
import ai
import ai.types.usage


def _fake_stream_result(text: str = "ok") -> ModelStreamResult:
    return ModelStreamResult(
        text=text,
        tool_calls=[],
        reasoning="",
        usage=ai.types.usage.Usage(input_tokens=0, output_tokens=len(text)),
        finish_reason="stop",
        assistant_message=ai.assistant_message(text),
    )




def cfg(tmp_path: Path, prompts: Path) -> Config:
    return Config(
        agent_id="surface-agent",
        agent_dir=tmp_path / ".js" / "sessions" / "surface-agent",
        model="offline-test-model",
        provider_id=None,
        provider_base_url=None,
        provider_api_key=None,
        reasoning_effort=None,
        max_output_tokens=None,
        max_tool_iterations=5,
        max_bash_output_bytes=65536,
        max_tool_result_bytes=65536,
        fetch_timeout_s=5,
        debug_log=None,
        trace=False,
        history_file=tmp_path / ".history",
        sessions_dir=tmp_path / ".js" / "sessions" / "surface-agent",
        session_file=tmp_path / ".js" / "sessions" / "surface-agent" / "surface.jsonl",
        prompts_dir=prompts,
    )


def write_prompt_dir(tmp_path: Path, zero: str | None, *rest: tuple[str, str]) -> Path:
    prompts = tmp_path / "prompts"
    prompts.mkdir(parents=True)
    if zero is not None:
        (prompts / "00-tools.md").write_text(zero, encoding="utf-8")
    for name, text in rest:
        (prompts / name).write_text(text, encoding="utf-8")
    return prompts


def names(registry) -> list[str]:
    return [tool.name for tool in registry.tools]


def test_registry_selection_handles_empty_globs_aliases_unknowns_and_dedupe():
    full = build_default_registry()

    assert names(select([])) == []
    assert names(select(["*"])) == names(full)

    fs_names = set(names(select(["fs_*"])))
    assert fs_names == {"fs_search"}
    assert "todo_read" not in fs_names

    assert names(select(["todo_*"])) == ["todo_write", "todo_read"]
    assert names(select(["grep"])) == []
    assert names(select(["read", "Read", "fs_read", "unknown", "read"])) == ["read"]
    assert names(select(["autocoder", "commit"])) == ["autocoder", "commit"]
    assert names(select(["auto*"])) == ["autocoder"]


def test_frontmatter_tools_are_parsed_and_stripped_from_prompt(tmp_path):
    prompts = write_prompt_dir(
        tmp_path,
        "---\ntools:\n  - todo_*\n---\n00 body\n",
        ("01-first.md", "FIRST\n"),
        ("02-second.md", "SECOND\n"),
    )

    spec = persona.load_prompt_spec(prompts)

    assert spec.tool_selectors == ("todo_*",)
    assert spec.system == "00 body\n\nFIRST\n\nSECOND\n"
    assert "---" not in spec.system


def test_frontmatter_only_absent_tools_empty_list_and_missing_00_default_none(tmp_path):
    frontmatter_only = write_prompt_dir(tmp_path / "front", "---\ntools: []\n---\n", ("01.md", "BODY\n"))
    no_tools_key = write_prompt_dir(tmp_path / "nokey", "---\nname: x\n---\nBODY\n")
    missing_zero = write_prompt_dir(tmp_path / "missing", None, ("01.md", "BODY\n"))

    assert persona.load_prompt_spec(frontmatter_only).tool_selectors == ()
    assert persona.load_prompt_spec(frontmatter_only).system == "BODY\n"
    assert persona.load_prompt_spec(no_tools_key).tool_selectors == ()
    assert persona.load_prompt_spec(missing_zero).tool_selectors == ()


def test_frontmatter_malformed_yaml_fails_clear(tmp_path):
    prompts = write_prompt_dir(tmp_path, "---\ntools: [\n---\nBODY\n")

    with pytest.raises(ValueError, match="invalid YAML frontmatter"):
        persona.load_prompt_spec(prompts)


def test_runtime_omits_tools_when_agent_selection_is_empty(monkeypatch, tmp_path):
    prompts = write_prompt_dir(tmp_path, None, ("01.md", "SYSTEM\n"))
    calls: list[dict] = []

    def stream_stub(**kwargs):
        calls.append(kwargs)
        return _fake_stream_result("NO_TOOLS_OK")

    monkeypatch.setattr(runtime.model_client, "stream_model", stream_stub)
    messages = [{"role": "user", "content": "hi"}]

    runtime.run_turn(
        cfg(tmp_path, prompts),
        persona.load_prompt(prompts),
        messages,
        runtime.Telemetry(None),
        tool_registry=select([]),
        trace_override=False,
    )

    assert calls[0].get("tools") is None
    assert messages[-1] == {"role": "assistant", "content": "NO_TOOLS_OK"}


def test_runtime_dispatch_rejects_unselected_tool_cleanly(tmp_path):
    registry = select(["todo_read"])

    _, result = runtime._dispatch(
        "read",
        '{"path":"x"}',
        runtime.Telemetry(None),
        cap_bytes=4096,
        registry=registry,
        tool_context=ToolContext(cwd=tmp_path),
    )

    assert result.startswith("ERROR: no tool named read; use todo_read")
