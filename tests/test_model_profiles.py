"""Per-model tool surfaces: agent.yaml `models:` and the TOOL_PROFILES setting."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from js import model_metadata, model_profiles, persona, runtime, settings
from js.cli_options import RunOptions, invocation
from js.config import Config
from js.toolkit import ToolContext
from js.toolkit.meta import task
from js.toolkit.registry import TurnToolSurface, build_default_registry

from test_agent_model_selection import _fake_stream_result, _make_cfg, _write_agent_dir

FOURTEEN = ("read", "write", "fs_search", "remove", "patch", "undo", "shell", "fetch",
            "plan", "skill", "task", "lsp", "notebook_edit", "jev")


@pytest.fixture(autouse=True)
def fresh_warnings(monkeypatch):
    monkeypatch.setattr(model_profiles, "_warned_empty", set())


def _agent(tmp_path: Path, models: list | None = None) -> Path:
    prompts = tmp_path / "prompts" / "surface-agent"
    prompts.mkdir(parents=True)
    manifest: dict = {"tools": [f"{name}:eager" for name in FOURTEEN]}
    if models is not None:
        manifest["models"] = models
    (prompts / "agent.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    (prompts / "01-prompt.md").write_text("SURFACE AGENT\n", encoding="utf-8")
    return prompts


def _cfg(tmp_path: Path, prompts: Path, *, model: str = "qwen3-coder", profiles: list | None = None) -> Config:
    store = {} if profiles is None else {"TOOL_PROFILES": profiles}
    return Config(
        agent_id="surface-agent",
        agent_dir=tmp_path / ".js" / "sessions" / "surface-agent",
        model=model,
        provider_id="ollama",
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
        settings=store,
    )


def _selected(spec: persona.PromptSpec) -> set[str]:
    return {tool.name for tool in build_default_registry().select(spec.tool_selectors).tools}


def _surface(spec: persona.PromptSpec, cwd: Path) -> set[str]:
    """The tools published on the first model call: eager tools, plus
    tool_discovery when skills, lazy tools or MCP servers are on the surface."""
    return {tool.name for tool in TurnToolSurface(build_default_registry().select(spec.tool_selectors), cwd).tools}


def test_the_agent_without_a_matching_profile_keeps_its_fourteen_tools(tmp_path):
    prompts = _agent(tmp_path, [{"match": "gpt", "tools": ["shell:eager"]}])

    spec = persona.load_configured_prompt_spec(_cfg(tmp_path, prompts), expand=False)

    assert _selected(spec) == set(FOURTEEN)
    assert spec.model_profile is None


def test_replace_keeps_shell_and_the_core_skill_and_tool_discovery(tmp_path):
    prompts = _agent(tmp_path, [{"match": "QWEN", "mode": "replace", "tools": ["shell:eager"]}])

    spec = persona.load_configured_prompt_spec(_cfg(tmp_path, prompts), expand=False)

    assert _surface(spec, tmp_path) == {"shell", "skill", "tool_discovery"}


def test_replace_without_core_is_only_the_profile_tools(tmp_path):
    prompts = _agent(tmp_path, [{"match": "qwen", "tools": ["shell:eager"], "core": False}])

    spec = persona.load_configured_prompt_spec(_cfg(tmp_path, prompts), expand=False)

    assert _surface(spec, tmp_path) == {"shell"}


def test_replace_keeps_the_tools_yaml_core_tag_when_it_is_defined(tmp_path):
    (tmp_path / ".js").mkdir(exist_ok=True)
    (tmp_path / ".js" / "tools.yaml").write_text("tags:\n  core: [read:eager]\n", encoding="utf-8")
    prompts = _agent(tmp_path, [{"match": "qwen", "tools": ["shell:eager"]}])

    spec = persona.load_configured_prompt_spec(_cfg(tmp_path, prompts), expand=False)

    assert _selected(spec) == {"shell", "read"}


def test_prepend_bans_ahead_of_the_agent_chain(tmp_path):
    prompts = _agent(tmp_path, [{"match": "qwen", "mode": "prepend", "tools": ["remove:ban", "undo:ban"]}])

    spec = persona.load_configured_prompt_spec(_cfg(tmp_path, prompts), expand=False)

    assert _selected(spec) == set(FOURTEEN) - {"remove", "undo"}


def test_match_is_tested_against_the_provider_id_too(tmp_path):
    prompts = _agent(tmp_path, [{"match": ["ollama"], "tools": ["shell:eager"], "core": False}])

    spec = persona.load_configured_prompt_spec(_cfg(tmp_path, prompts, model="other"), expand=False)

    assert _selected(spec) == {"shell"}


def test_tool_profiles_setting_applies_when_the_agent_has_no_models(tmp_path):
    prompts = _agent(tmp_path)
    profiles = [{"match": "qwen", "tools": ["read:eager"], "core": False}]

    spec = persona.load_configured_prompt_spec(_cfg(tmp_path, prompts, profiles=profiles), expand=False)

    assert _selected(spec) == {"read"}


def test_a_matching_agent_entry_beats_the_tool_profiles_setting(tmp_path):
    prompts = _agent(tmp_path, [{"match": "qwen", "tools": ["shell:eager"], "core": False}])
    profiles = [{"match": "qwen", "tools": ["read:eager"], "core": False}]

    spec = persona.load_configured_prompt_spec(_cfg(tmp_path, prompts, profiles=profiles), expand=False)

    assert _selected(spec) == {"shell"}


def test_tools_flag_overrides_every_profile(tmp_path):
    prompts = _agent(tmp_path, [{"match": "qwen", "tools": ["shell:eager"], "core": False}])
    profiles = [{"match": "qwen", "tools": ["read:eager"], "core": False}]

    with invocation(RunOptions(tools="fetch")):
        spec = persona.load_configured_prompt_spec(_cfg(tmp_path, prompts, profiles=profiles), expand=False)

    assert _selected(spec) == {"fetch"}


def test_a_profile_that_leaves_no_tools_is_ignored_with_one_warning(tmp_path, capsys):
    prompts = _agent(tmp_path, [{"match": "qwen", "tools": ["*:ban"], "core": False,
                                 "sampling": {"temperature": 0.1}}])
    cfg = _cfg(tmp_path, prompts)

    first = persona.load_configured_prompt_spec(cfg, expand=False)
    second = persona.load_configured_prompt_spec(cfg, expand=False)

    assert _selected(first) == _selected(second) == set(FOURTEEN)
    assert first.sampling == {}
    assert capsys.readouterr().err.count("qwen") == 1


def test_profile_prompt_sampling_and_reasoning_apply(tmp_path):
    prompts = _agent(tmp_path, [{"match": "qwen", "prompt": "small", "sampling": {"temperature": 0.2},
                                 "reasoning": "low"}])
    small = tmp_path / "prompts" / "small"
    small.mkdir()
    (small / "01-prompt.md").write_text("SMALL PERSONA\n", encoding="utf-8")

    spec = persona.load_configured_prompt_spec(_cfg(tmp_path, prompts), expand=False)

    assert "SMALL PERSONA" in spec.system and "SURFACE AGENT" not in spec.system
    assert spec.sampling == {"temperature": 0.2}
    assert spec.reasoning_effort == "low"


def test_for_model_reloads_for_the_model_the_run_settled_on(tmp_path):
    prompts = _agent(tmp_path, [{"match": "qwen", "tools": ["shell:eager"], "core": False}])
    cfg = _cfg(tmp_path, prompts, model="startup-model")
    spec = persona.load_configured_prompt_spec(cfg, expand=False)
    assert _selected(spec) == set(FOURTEEN)

    settled = persona.for_model(spec, replace(cfg, model="qwen3-coder"))

    assert _selected(settled) == {"shell"}
    assert persona.for_model(settled, replace(cfg, model="qwen3-coder")) is settled


@pytest.mark.parametrize("raw", [
    {"tools": ["shell:eager"]},
    {"match": "", "tools": ["shell:eager"]},
    {"match": "qwen", "mode": "append"},
    {"match": "qwen", "tools": ["shell"]},
    {"match": "qwen", "core": "no"},
    {"match": "qwen", "prompt": "../elsewhere"},
    {"match": "qwen", "colour": "blue"},
])
def test_malformed_profiles_fail_the_agent_load_and_the_setting(tmp_path, raw):
    prompts = _agent(tmp_path, [raw])

    with pytest.raises(ValueError):
        persona.load_configured_prompt_spec(_cfg(tmp_path, prompts), expand=False)
    _value, error = settings.coerce_value(settings.SPEC_BY_KEY["TOOL_PROFILES"], json.dumps([raw]))
    assert error is not None


def test_a_subagent_resolves_its_profile_from_its_own_model(monkeypatch, tmp_path):
    manifest = yaml.safe_dump({
        "model": "child-model",
        "tools": ["read:eager", "fetch:eager"],
        "models": [{"match": "child", "tools": ["read:eager"], "core": False}],
    })
    _write_agent_dir(tmp_path / "prompts", manifest=manifest)
    parent_prompts = _write_agent_dir(tmp_path / "prompts", agent_id="parent")
    cfg = _make_cfg(tmp_path, parent_prompts, model="parent-model")
    seen: dict = {}

    def stream_stub(**kwargs):
        seen.update(kwargs)
        return _fake_stream_result("CHILD_OK")

    monkeypatch.setattr(runtime.model_client, "stream_model_async", stream_stub)
    monkeypatch.setattr(model_metadata, "resolve_max_output", lambda _model, _provider_id: None)
    context = ToolContext(cwd=tmp_path)
    context.config = cfg

    assert "CHILD_OK" in task(["do the child work"], agent_id="worker", context=context)
    assert seen["model_id"] == "child-model"
    assert {tool.name for tool in seen["tools"] or ()} == {"read"}
