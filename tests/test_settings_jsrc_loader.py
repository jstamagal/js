"""Tests for jsrc settings, the js home, provider env, and runtime plumbing."""

from __future__ import annotations

import copy

from pathlib import Path

import pytest

from js import runtime, session_store, settings
from js.config import Config, from_env
from js import messages as msgs


def _env_dirs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    config_home = tmp_path / "config"
    data_home = tmp_path / "data"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    for spec in settings.REGISTRY:
        if spec.env:
            monkeypatch.delenv(spec.env, raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    js_home = tmp_path / "home" / ".js"
    return js_home, js_home


def _build_config(
    tmp_path: Path,
    *,
    model: str = "offline-test-model",
    provider_id=None,
    provider_base_url=None,
    provider_api_key=None,
    provider_headers: dict[str, str] | None = None,
    max_output_tokens=None,
):
    return Config(
        agent_id="test-agent",
        agent_dir=tmp_path / "data" / "js" / "sessions" / "test-agent",
        model=model,
        provider_id=provider_id,
        provider_base_url=provider_base_url,
        provider_api_key=provider_api_key,
        reasoning_effort=None,
        max_output_tokens=max_output_tokens,
        max_tool_iterations=5,
        max_bash_output_bytes=65536,
        max_tool_result_bytes=65536,
        fetch_timeout_s=5,
        debug_log=None,
        trace=False,
        history_file=tmp_path / "data" / "js" / "sessions" / "test-agent" / ".history",
        sessions_dir=tmp_path / "data" / "js" / "sessions" / "test-agent",
        session_file=tmp_path / "data" / "js" / "sessions" / "test-agent" / "x.jsonl",
        prompts_dir=tmp_path / "prompts",
        provider_headers=provider_headers or {},
    )


def test_collect_settings_uses_built_in_default_when_no_file_or_env():
    out = settings.collect_settings(config_paths=[], env={})
    assert out["MODEL"] == settings.default_value("MODEL")
    assert out["TURN_ROUNDS_MAX"] == settings.default_value("TURN_ROUNDS_MAX")
    assert out["REPLY_TOOL_CALLS_MAX"] == settings.default_value("REPLY_TOOL_CALLS_MAX")


def test_collect_settings_reads_jsrc_set_lines(tmp_path):
    cfg = tmp_path / "jsrc"
    cfg.write_text("set MODEL file-model\n", encoding="utf-8")

    out = settings.collect_settings(config_paths=[cfg], env={})

    assert out["MODEL"] == "file-model"


def test_save_preserves_disabled_reasoning(tmp_path):
    from js import setcmd

    store = settings.seed_defaults()
    result = setcmd.apply_set(store, "EFFORT", "off")
    assert result.error is None
    path = tmp_path / "jsrc"

    settings.save_settings_to_jsrc(path, store, keys=["EFFORT"])

    loaded = settings.collect_settings([path], env={})
    assert loaded["EFFORT"] == store["EFFORT"] == "none"


def test_jsrc_rejects_registered_non_map_subkeys_without_mutating(tmp_path):
    cfg = tmp_path / "jsrc"
    cfg.write_text(
        "set TOOL_ALIASES.foo bar\n"
        "set MODEL file-model\n",
        encoding="utf-8",
    )
    live_settings = settings.seed_defaults()
    aliases_before = copy.deepcopy(live_settings.get("TOOL_ALIASES"))

    warnings = settings.load_jsrc_files([cfg], live_settings)

    assert warnings == [msgs.SCRIPT_LINE_FAILED.text(
        path=cfg, lineno=1, error=msgs.UNKNOWN_SETTING.text(key="TOOL_ALIASES.foo"))]
    assert live_settings["MODEL"] == "file-model"
    assert live_settings.get("TOOL_ALIASES") == aliases_before


def test_collect_settings_applies_env_after_file(tmp_path):
    cfg = tmp_path / "jsrc"
    cfg.write_text("set MODEL file-model\n", encoding="utf-8")

    out = settings.collect_settings(config_paths=[cfg], env={"JS_MODEL": "env-model"})

    assert out["MODEL"] == "env-model"


def test_collect_settings_cli_extras_win_over_env(tmp_path):
    cfg = tmp_path / "jsrc"
    cfg.write_text("set MODEL file-model\n", encoding="utf-8")

    out = settings.collect_settings(
        config_paths=[cfg],
        env={"JS_MODEL": "env-model"},
        extras=["MODEL=cli-model"],
    )

    assert out["MODEL"] == "cli-model"


def test_collect_settings_layers_global_project_and_local_jsrc(tmp_path):
    global_cfg = tmp_path / "global" / "jsrc"
    project_cfg = tmp_path / "project" / ".js" / "jsrc"
    local_cfg = tmp_path / "project" / ".js" / "jsrc.local"
    global_cfg.parent.mkdir(parents=True)
    project_cfg.parent.mkdir(parents=True)
    global_cfg.write_text("set MODEL global-model\nset FETCH_TIMEOUT 20\nset INLINE_CODE_TIMEOUT 222\n", encoding="utf-8")
    project_cfg.write_text("set MODEL project-model\nset TURN_ROUNDS_MAX 7\n", encoding="utf-8")
    local_cfg.write_text("set MODEL local-model\n", encoding="utf-8")

    out = settings.collect_settings(config_paths=[global_cfg, project_cfg, local_cfg], env={})

    assert out["MODEL"] == "local-model"
    assert out["FETCH_TIMEOUT"] == 20
    assert out["INLINE_CODE_TIMEOUT"] == 222
    assert out["TURN_ROUNDS_MAX"] == 7


def test_from_env_uses_the_js_home_and_writes_no_global_jsrc(monkeypatch, tmp_path):
    config_home, data_home = _env_dirs(monkeypatch, tmp_path)
    # Explicit pin (PROVIDER / JS_PROVIDER) authorizes reading the provider's
    # native env key: an env key alone would create no route (ruling #1).
    monkeypatch.setenv("JS_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")

    cfg = from_env(save_session=True)

    assert cfg.provider_id == "deepseek"
    assert cfg.provider_api_key == "sk-test"
    assert cfg.sessions_dir == data_home / "sessions" / session_store.slug(Path.cwd())
    # Reading config writes nothing; the CLI entry point writes the first jsrc.
    assert not (config_home / "jsrc").exists()
    assert not (tmp_path / "config").exists()
    assert not (tmp_path / "data").exists()


def test_from_env_reads_global_jsrc(monkeypatch, tmp_path):
    config_home, _data_home = _env_dirs(monkeypatch, tmp_path)
    global_cfg = config_home / "jsrc"
    global_cfg.parent.mkdir(parents=True)
    global_cfg.write_text("set MODEL global-model\nset TURN_ROUNDS_MAX 8\n", encoding="utf-8")

    cfg = from_env(save_session=False)

    assert cfg.model == "global-model"
    assert cfg.max_tool_iterations == 8
    assert global_cfg.read_text(encoding="utf-8").startswith("set MODEL global-model")


def test_explicit_default_model_extra_beats_the_agent_model(tmp_path):
    from types import SimpleNamespace

    from js import cli, logins

    model_id = settings.default_value("MODEL")
    provider_id, _, _ = model_id.partition("/")
    logins.save_login(logins.Login(provider_id=provider_id, provider_api_key="fixture-key"))
    cfg = from_env(cwd=tmp_path, save_session=False, extras=[f"MODEL={model_id}"])
    agent = SimpleNamespace(model="agent-model")

    active = cli._apply_agent_model(cfg, agent, None)

    assert active.model == cfg.model


def test_presets_layer_over_base_in_order(monkeypatch, tmp_path):
    config_home, _data_home = _env_dirs(monkeypatch, tmp_path)
    js_dir = config_home
    js_dir.mkdir(parents=True)
    (js_dir / "jsrc").write_text("set MODEL base-model\nset FETCH_TIMEOUT 5\n", encoding="utf-8")
    (js_dir / "jsrc.fast").write_text("set MODEL fast-model\n", encoding="utf-8")
    (js_dir / "jsrc.slow").write_text("set MODEL slow-model\nset FETCH_TIMEOUT 99\n", encoding="utf-8")

    # No preset: base wins.
    assert from_env(save_session=False).model == "base-model"

    # Last preset wins on overlap; earlier preset's non-overlapping keys persist.
    cfg = from_env(save_session=False, presets=["fast", "slow"])
    assert cfg.model == "slow-model"
    assert cfg.fetch_timeout_s == 99

    cfg2 = from_env(save_session=False, presets=["slow", "fast"])
    assert cfg2.model == "fast-model"
    assert cfg2.fetch_timeout_s == 99  # from slow; fast didn't set it


@pytest.mark.parametrize("presets", [[], ["fast"]])
def test_ignoring_global_config_from_home_excludes_global_files(tmp_path, presets):
    config_dir = tmp_path / ".js"
    config_dir.mkdir()
    (config_dir / "jsrc").write_text("set SHELL_WAIT 47\n", encoding="utf-8")
    (config_dir / "jsrc.fast").write_text("set SHELL_WAIT 49\n", encoding="utf-8")

    cfg = from_env(cwd=tmp_path, save_session=False, ignore_global_config=True, presets=presets)

    assert cfg.shell_wait_seconds == settings.default_value("SHELL_WAIT")


def test_ignoring_global_config_holds_under_another_name_for_home(tmp_path, monkeypatch):
    # The home directory reached by a second name is the same directory, so
    # its jsrc is still the global layer --ignore-global excludes.
    home = tmp_path / "home"
    (home / ".js").mkdir(parents=True)
    (home / ".js" / "jsrc").write_text("set SHELL_WAIT 47\n", encoding="utf-8")
    (home / ".js" / "jsrc.fast").write_text("set SHELL_WAIT 49\n", encoding="utf-8")
    second_name = tmp_path / "home-link"
    second_name.symlink_to(home, target_is_directory=True)
    monkeypatch.setenv("HOME", str(second_name))

    for presets in ([], ["fast"]):
        cfg = from_env(cwd=home, save_session=False, ignore_global_config=True, presets=presets)
        assert cfg.shell_wait_seconds == settings.default_value("SHELL_WAIT")


def test_allow_inline_code_opt_out_via_jsrc(monkeypatch, tmp_path):
    # Deliberate opt-out through the config knob.
    config_home, _data_home = _env_dirs(monkeypatch, tmp_path)
    global_cfg = config_home / "jsrc"
    global_cfg.parent.mkdir(parents=True)
    global_cfg.write_text("set INLINE_CODE off\n", encoding="utf-8")

    cfg = from_env(save_session=False)
    assert cfg.allow_inline_code is False


def test_js_model_provider_prefix_is_parsed_once(monkeypatch, tmp_path):
    _env_dirs(monkeypatch, tmp_path)
    # Pin the provider (an env prefix alone never authorizes a route); the prefix
    # must still be split exactly once, not double-stripped.
    monkeypatch.setenv("JS_PROVIDER", "ollama")
    monkeypatch.setenv("JS_MODEL", "ollama/hf.co/user/model:latest")

    cfg = from_env(save_session=False)

    assert cfg.provider_id == "ollama"
    assert cfg.model == "hf.co/user/model:latest"


def test_js_provider_base_and_key_env_names(monkeypatch, tmp_path):
    _env_dirs(monkeypatch, tmp_path)
    monkeypatch.setenv("JS_PROVIDER", "openai")
    monkeypatch.setenv("JS_MODEL", "custom-model")
    monkeypatch.setenv("JS_BASE_URL", "http://proxy.test/v1")
    monkeypatch.setenv("JS_API_KEY", "sk-proxy")

    cfg = from_env(save_session=False)

    assert cfg.provider_id == "openai"
    assert cfg.model == "custom-model"
    assert cfg.provider_base_url == "http://proxy.test/v1"
    assert cfg.provider_api_key == "sk-proxy"


def _fake_stream_result(text: str = "ok"):
    from js.model_client import ModelStreamResult
    import ai
    import ai.types.usage

    return ModelStreamResult(
        text=text,
        tool_calls=[],
        reasoning="",
        usage=ai.types.usage.Usage(input_tokens=0, output_tokens=len(text)),
        finish_reason="stop",
        assistant_message=ai.assistant_message(text),
    )


def test_provider_config_reaches_stream_model(monkeypatch, tmp_path):
    captured_kwargs: dict = {}

    def stream_stub(**kwargs):
        captured_kwargs.update(kwargs)
        return _fake_stream_result("ok")

    monkeypatch.setattr("js.runtime.model_client.stream_model_async", stream_stub)
    cfg = _build_config(
        tmp_path,
        provider_id="openai",
        provider_base_url="http://127.0.0.1:11434/v1",
        provider_api_key="ollama",
        provider_headers={"x-test": "1"},
    )
    from js.toolkit import ToolContext

    runtime.run_turn(
        cfg,
        "system",
        [{"role": "user", "content": "hi"}],
        runtime.Telemetry(None),
        trace_override=False,
        tool_context=ToolContext(cwd=tmp_path),
        suppress_output=True,
    )

    assert captured_kwargs.get("provider_id") == "openai"
    assert captured_kwargs.get("provider_base_url") == "http://127.0.0.1:11434/v1"
    assert captured_kwargs.get("provider_api_key") == "ollama"
    assert captured_kwargs.get("provider_headers") == {"x-test": "1"}


def test_loading_a_jsrc_with_old_names_leaves_it_as_written(tmp_path):
    path = tmp_path / "jsrc"
    original = "set model.id old-model\n"
    path.write_text(original)
    settings.collect_settings(config_paths=[path], env={})
    assert path.read_text() == original
    assert list(tmp_path.glob("jsrc.*")) == []
