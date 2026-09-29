"""tools.yaml chains: noun:modifier entries, tags, first match wins."""
from __future__ import annotations

import json
from pathlib import Path

import ai
import ai.types.usage
import pytest

from js import cli, persona, runtime
from js.config import Config
from js.model_client import ModelStreamResult, ModelToolCall
from js.toolkit import ToolContext, policy
from js.toolkit.registry import build_default_registry

SPEC_TOOLS_YAML = """\
tags:
  code_editor:
    - "*:ban"
    - read:eager
    - write:eager
    - patch:eager
    - undo:eager
    - docs_search:lazy
  code_hacker:
    - shell:eager
    - tag:code_editor
  code_warrior:
    - tag:code_hacker
    - ast_search:eager
    - docs_search:eager

ban:
  shell:
    - "rm -rf"
    - "git reset --hard"
    - "find /"
  fetch:
    - "*://www.pornhub.com/*"

skills:
  - productivity:grilling
"""


def _config(tmp_path: Path, text: str = SPEC_TOOLS_YAML) -> policy.ToolsConfig:
    path = tmp_path / "tools.yaml"
    path.write_text(text, encoding="utf-8")
    return policy.load_tools_config(path)


def _states(registry, entries, config) -> dict[str, str | None]:
    rules = policy.expand(entries, config, "test")
    return {d.tool.name: d.modifier for d in policy.resolve(registry.tools, rules)}


def test_spec_example_code_hacker_has_shell_eager_above_the_editor_drop(tmp_path):
    config = _config(tmp_path)
    full = build_default_registry()

    states = _states(full, ["tag:code_hacker"], config)

    assert states["shell"] == "eager"
    assert {states[name] for name in ("read", "write", "patch", "undo")} == {"eager"}
    assert states["docs_search"] == "lazy"
    assert states["fetch"] == "ban"
    assert states["task"] == "ban"


def test_code_editor_bans_shell(tmp_path):
    states = _states(build_default_registry(), ["tag:code_editor"], _config(tmp_path))

    assert states["shell"] == "ban"
    assert states["read"] == "eager"


def test_a_name_allows_past_a_glob_drop_and_names_resolve_by_position(tmp_path):
    states = _states(build_default_registry(), ["tag:code_warrior"], _config(tmp_path))

    assert states["ast_search"] == "eager"
    assert states["shell"] == "eager"
    # code_editor's docs_search:lazy names it before code_warrior's eager does.
    assert states["docs_search"] == "lazy"
    assert states["fetch"] == "ban"


def test_globs_resolve_by_position_among_themselves():
    full = build_default_registry()
    states = _states(full, ["*_search:lazy", "*:ban"], policy.ToolsConfig())
    assert states["fs_search"] == "lazy"
    assert states["read"] == "ban"

    states = _states(full, ["*:ban", "*_search:lazy"], policy.ToolsConfig())
    assert states["fs_search"] == "ban"


def test_select_splits_eager_lazy_and_leaves_banned_and_unnamed_tools_off(tmp_path):
    config = _config(tmp_path)
    full = build_default_registry()

    selected = full.select(["tag:code_hacker"], config=config)

    names = {tool.name for tool in selected.tools}
    assert names == {"shell", "read", "write", "patch", "undo", "docs_search"}
    assert selected.lazy == {"docs_search"}

    unnamed = full.select(["read:eager"], config=config)
    assert {tool.name for tool in unnamed.tools} == {"read"}


def test_intrinsic_read_only_tag_with_a_ban_ahead_of_it():
    full = build_default_registry()
    read_only = {tool.name for tool in full.tools if tool.read_only}
    assert {"read", "fs_search", "tavily_search"} <= read_only
    assert not {"write", "shell", "patch", "remove", "task"} & read_only

    states = _states(full, ["tavily_search:ban", "tag:read_only"], policy.ToolsConfig())

    assert states["tavily_search"] == "ban"
    assert all(states[name] == "eager" for name in read_only - {"tavily_search"})
    assert states["write"] is None


def test_intrinsic_tag_takes_a_modifier():
    full = build_default_registry()
    selected = full.select(["tag:read_only:lazy"], config=policy.ToolsConfig())

    assert selected.lazy == {tool.name for tool in full.tools if tool.read_only}


@pytest.mark.parametrize("entry", ["read", "read:sometimes", ":eager", "tag:code_editor:lazy"])
def test_malformed_entries_are_refused(tmp_path, entry):
    with pytest.raises(policy.ToolPolicyError):
        policy.expand([entry], _config(tmp_path), "test")


def test_unknown_tag_and_tag_cycles_are_refused(tmp_path):
    config = _config(tmp_path, "tags:\n  a: [tag:b]\n  b: [tag:a]\n")

    with pytest.raises(policy.ToolPolicyError):
        policy.expand(["tag:nope"], config, "test")
    with pytest.raises(policy.ToolPolicyError):
        policy.expand(["tag:a"], config, "test")


def test_bare_selector_in_an_agent_manifest_fails_the_prompt_load(tmp_path):
    prompts = tmp_path / "agent"
    prompts.mkdir()
    (prompts / "agent.yaml").write_text("tools:\n  - read\n", encoding="utf-8")
    (prompts / "01.md").write_text("SYSTEM\n", encoding="utf-8")

    with pytest.raises(ValueError):
        persona.load_prompt_spec(prompts)


def test_a_tool_the_chain_does_not_name_is_not_in_the_discovery_catalog(tmp_path):
    full = build_default_registry()
    config = policy.ToolsConfig()

    surface = full.select(["read:eager"], config=config).lazy_surface(tmp_path)
    assert "fetch" not in {tool.name for tool in surface.tools}
    assert surface.discover(load="native:fetch").startswith("ERROR")

    surface = full.select(["read:eager", "fetch:lazy"], config=config).lazy_surface(tmp_path)
    assert "fetch" not in {tool.name for tool in surface.tools}
    assert [item.name for item in surface.catalog() if item.kind == "native"] == ["fetch"]
    assert json.loads(surface.discover(load="native:fetch"))["loaded"] == ["fetch"]
    assert "fetch" in {tool.name for tool in surface.tools}


def _cfg(tmp_path: Path) -> Config:
    return Config(
        agent_id="policy-agent",
        agent_dir=tmp_path / ".js" / "sessions" / "policy-agent",
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
        sessions_dir=tmp_path / ".js" / "sessions" / "policy-agent",
        session_file=tmp_path / ".js" / "sessions" / "policy-agent" / "s.jsonl",
        prompts_dir=tmp_path / "prompts" / "policy-agent",
    )


def _tool_call(name: str, args: dict) -> ModelStreamResult:
    return ModelStreamResult(
        text="",
        tool_calls=[ModelToolCall(id="call_1", name=name, arguments=json.dumps(args))],
        reasoning="",
        usage=ai.types.usage.Usage(input_tokens=0, output_tokens=1),
        finish_reason="tool_calls",
        assistant_message=ai.assistant_message(""),
    )


def _stop(text: str) -> ModelStreamResult:
    return ModelStreamResult(
        text=text,
        tool_calls=[],
        reasoning="",
        usage=ai.types.usage.Usage(input_tokens=0, output_tokens=1),
        finish_reason="stop",
        assistant_message=ai.assistant_message(text),
    )


def _tool_results(messages: list[dict]) -> list[str]:
    return [str(m.get("content")) for m in messages if m.get("role") == "tool"]


@pytest.fixture
def offline_model(monkeypatch):
    monkeypatch.setattr(runtime.model_metadata, "accepts_image_input", lambda *a, **k: False)
    monkeypatch.setattr(runtime, "_resolve_context_window", lambda *a, **k: 1_000_000)
    monkeypatch.setattr(runtime.model_metadata, "resolve_max_output", lambda *a, **k: 4096)


def test_an_eager_tool_is_published_on_the_first_call_and_runs(monkeypatch, tmp_path, offline_model):
    (tmp_path / "note.txt").write_text("EAGER CONTENT\n", encoding="utf-8")
    published: list[set[str]] = []
    replies = iter([_tool_call("read", {"file_path": "note.txt"}), _stop("done")])

    def stream(**kwargs):
        published.append({tool.name for tool in kwargs.get("tools") or ()})
        return next(replies)

    monkeypatch.setattr(runtime.model_client, "stream_model_async", stream)
    messages = [{"role": "user", "content": "read it"}]
    registry = build_default_registry().select(["read:eager"], config=policy.ToolsConfig())

    runtime.run_turn(_cfg(tmp_path), "system", messages, runtime.Telemetry(None),
                     tool_registry=registry, tool_context=ToolContext(cwd=tmp_path),
                     trace_override=False)

    assert "read" in published[0]
    results = _tool_results(messages)
    assert len(results) == 1 and "EAGER CONTENT" in results[0]


def test_tools_command_prints_each_tool_with_its_deciding_entry(tmp_path, capsys):
    policy.tools_config_path().parent.mkdir(parents=True, exist_ok=True)
    policy.tools_config_path().write_text(SPEC_TOOLS_YAML, encoding="utf-8")
    cfg = _cfg(tmp_path)
    state = {"tool_selectors": ("tag:code_hacker",), "settings": {}}

    assert cli._handle_command("/tools", state, cfg) is True

    rows = {line.split()[0]: line for line in capsys.readouterr().out.splitlines() if line.strip()}
    assert "eager" in rows["shell"].split() and "shell:eager" in rows["shell"]
    assert "ban" in rows["fetch"].split() and '*:ban' in rows["fetch"]
    assert "tag:code_editor" in rows["fetch"]
    assert "lazy" in rows["docs_search"].split()


def _run_shell_rm(monkeypatch, tmp_path, registry) -> tuple[Path, list[str]]:
    victim = tmp_path / "x"
    victim.mkdir()
    replies = iter([_tool_call("shell", {"command": f"rm -rf {victim}"}), _stop("done")])
    monkeypatch.setattr(runtime.model_client, "stream_model_async", lambda **kwargs: next(replies))
    messages = [{"role": "user", "content": "clean up"}]
    runtime.run_turn(_cfg(tmp_path), "system", messages, runtime.Telemetry(None),
                     tool_registry=registry, tool_context=ToolContext(cwd=tmp_path),
                     trace_override=False)
    return victim, _tool_results(messages)


def test_banned_shell_argument_is_refused_before_it_runs(monkeypatch, tmp_path, offline_model):
    policy.tools_config_path().parent.mkdir(parents=True, exist_ok=True)
    policy.tools_config_path().write_text(SPEC_TOOLS_YAML, encoding="utf-8")
    registry = build_default_registry().select(["shell:eager"])

    victim, results = _run_shell_rm(monkeypatch, tmp_path, registry)

    assert victim.is_dir()
    assert len(results) == 1
    refusal = results[0].splitlines()[0]
    assert refusal.startswith("ERROR") and "rm -rf" in refusal


def test_the_same_shell_call_runs_without_a_ban(monkeypatch, tmp_path, offline_model):
    registry = build_default_registry().select(["shell:eager"], config=policy.ToolsConfig())

    victim, results = _run_shell_rm(monkeypatch, tmp_path, registry)

    assert not victim.exists()
    assert not results[0].startswith("ERROR")


def test_ban_patterns_match_substrings_and_whole_string_globs():
    bans = {"shell": ("git reset --hard",), "fetch": ("*://www.pornhub.com/*",)}

    assert policy.argument_refusal("shell", {"command": "cd x && GIT RESET --HARD HEAD"}, bans)
    assert policy.argument_refusal("shell", {"command": "git reset --soft HEAD"}, bans) is None
    assert policy.argument_refusal("fetch", {"url": "https://www.pornhub.com/x"}, bans)
    assert policy.argument_refusal("fetch", {"url": "https://example.com/?q=www.pornhub.com/"}, bans) is None
    assert policy.argument_refusal("read", {"file_path": "git reset --hard"}, bans) is None
