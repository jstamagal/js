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


def write_prompt_dir(
    tmp_path: Path,
    zero: str | None,
    *rest: tuple[str, str],
    zero_name: str = "00-tools.yaml",
) -> Path:
    prompts = tmp_path / "prompts"
    prompts.mkdir(parents=True)
    if zero is not None:
        (prompts / zero_name).write_text(zero, encoding="utf-8")
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
    # prompt-dir agents are selectable by name and reachable via a prefix glob
    # (discovered from prompts/, so adding/removing an agent dir never snaps this).
    prompt_agents = sorted(p.name for p in Path("prompts").iterdir() if p.is_dir())
    assert prompt_agents, "prompts/ should expose at least one agent dir"
    assert names(select(prompt_agents)) == prompt_agents
    assert prompt_agents[0] in names(select([prompt_agents[0][:-2] + "*"]))


def test_yaml_tools_manifest_is_parsed_and_not_prompt_body(tmp_path):
    prompts = write_prompt_dir(
        tmp_path,
        (
            "tools:\n"
            "  - todo_*\n"
            "model: primary-model\n"
            "secondary_model: backup-model\n"
            "sampling:\n"
            "  temperature: 0.2\n"
        ),
        ("01-first.md", "FIRST\n"),
        ("02-second.md", "SECOND\n"),
    )

    spec = persona.load_prompt_spec(prompts)

    assert spec.tool_selectors == ("todo_*",)
    assert spec.model == "primary-model"
    assert spec.secondary_model == "backup-model"
    assert spec.sampling == {"temperature": 0.2}
    assert spec.system == "FIRST\n\nSECOND\n"
    assert "tools:" not in spec.system


def test_yaml_zero_file_wins_over_legacy_markdown_zero_file(tmp_path, capsys):
    prompts = write_prompt_dir(
        tmp_path,
        "tools:\n  - todo_read\n",
        ("01.md", "BODY\n"),
    )
    (prompts / "00-tools.md").write_text(
        "---\ntools:\n  - shell\n---\nLEGACY BODY\n",
        encoding="utf-8",
    )

    spec = persona.load_prompt_spec(prompts)

    assert spec.tool_selectors == ("todo_read",)
    assert spec.system == "BODY\n"
    assert capsys.readouterr().err == ""


def test_yaml_manifest_absent_tools_empty_list_and_missing_00_default_none(tmp_path):
    manifest_only = write_prompt_dir(tmp_path / "manifest", "tools: []\n", ("01.md", "BODY\n"))
    no_tools_key = write_prompt_dir(tmp_path / "nokey", "name: x\n", ("01.md", "BODY\n"))
    missing_zero = write_prompt_dir(tmp_path / "missing", None, ("01.md", "BODY\n"))

    assert persona.load_prompt_spec(manifest_only).tool_selectors == ()
    assert persona.load_prompt_spec(manifest_only).system == "BODY\n"
    assert persona.load_prompt_spec(no_tools_key).tool_selectors == ()
    assert persona.load_prompt_spec(missing_zero).tool_selectors == ()


def test_yaml_manifest_malformed_yaml_fails_clear(tmp_path):
    prompts = write_prompt_dir(tmp_path, "tools: [\n", ("01.md", "BODY\n"))

    with pytest.raises(ValueError, match="invalid YAML manifest"):
        persona.load_prompt_spec(prompts)


def test_legacy_frontmatter_zero_file_still_loads_tools_once(tmp_path, capsys):
    prompts = write_prompt_dir(
        tmp_path,
        "---\ntools:\n  - shell\n---\nLEGACY BODY\n",
        ("01.md", "BODY\n"),
        zero_name="00-tools.md",
    )

    spec = persona.load_prompt_spec(prompts)

    assert spec.tool_selectors == ("shell",)
    assert spec.system == "LEGACY BODY\n\nBODY\n"
    first_note = capsys.readouterr().err
    assert "00-tools.md frontmatter manifests are deprecated" in first_note
    assert "00-tools.yaml" in first_note

    persona.load_prompt_spec(prompts)
    assert capsys.readouterr().err == ""


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
