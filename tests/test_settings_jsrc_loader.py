"""Tests for jsrc settings, platform dirs, provider env, and runtime plumbing."""

from __future__ import annotations

from pathlib import Path

import pytest

from js import runtime, settings
from js.config import Config, from_env


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
    return config_home, data_home


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
    assert out["model"]["id"] == settings.DEFAULT_MODEL
    assert out["limits"]["max_tool_iterations"] == settings.DEFAULT_MAX_TOOL_ITERATIONS


def test_collect_settings_reads_jsrc_set_lines(tmp_path):
    cfg = tmp_path / "jsrc"
    cfg.write_text("set model.id file-model\n", encoding="utf-8")

    out = settings.collect_settings(config_paths=[cfg], env={})

    assert out["model"]["id"] == "file-model"


def test_jsrc_rejects_registered_non_map_subkeys_without_mutating(tmp_path):
    cfg = tmp_path / "jsrc"
    cfg.write_text(
        "set tools.alias_profiles.foo bar\n"
        "set model.id file-model\n",
        encoding="utf-8",
    )
    live_settings = settings.seed_defaults()

    warnings = settings.load_jsrc_files([cfg], live_settings)

    assert warnings == [f"{cfg}:1: unknown knob: tools.alias_profiles.foo"]
    assert live_settings["model"]["id"] == "file-model"
    assert "alias_profiles" not in live_settings.get("tools", {})


def test_collect_settings_applies_env_after_file(tmp_path):
    cfg = tmp_path / "jsrc"
    cfg.write_text("set model.id file-model\n", encoding="utf-8")

    out = settings.collect_settings(config_paths=[cfg], env={"JS_MODEL": "env-model"})

    assert out["model"]["id"] == "env-model"


def test_collect_settings_cli_extras_win_over_env(tmp_path):
    cfg = tmp_path / "jsrc"
    cfg.write_text("set model.id file-model\n", encoding="utf-8")

    out = settings.collect_settings(
        config_paths=[cfg],
        env={"JS_MODEL": "env-model"},
        extras=["model.id=cli-model"],
    )

    assert out["model"]["id"] == "cli-model"


def test_collect_settings_layers_global_project_and_local_jsrc(tmp_path):
    global_cfg = tmp_path / "global" / "jsrc"
    project_cfg = tmp_path / "project" / ".js" / "jsrc"
    local_cfg = tmp_path / "project" / ".js" / "jsrc.local"
    global_cfg.parent.mkdir(parents=True)
    project_cfg.parent.mkdir(parents=True)
    global_cfg.write_text("set model.id global-model\nset limits.fetch_timeout_s 20\nset limits.inline_code_timeout_s 222\n", encoding="utf-8")
    project_cfg.write_text("set model.id project-model\nset limits.max_tool_iterations 7\n", encoding="utf-8")
    local_cfg.write_text("set model.id local-model\n", encoding="utf-8")

    out = settings.collect_settings(config_paths=[global_cfg, project_cfg, local_cfg], env={})

    assert out["model"]["id"] == "local-model"
    assert out["limits"]["fetch_timeout_s"] == 20
    assert out["limits"]["inline_code_timeout_s"] == 222
    assert out["limits"]["max_tool_iterations"] == 7


def test_write_default_template_creates_jsrc_once(tmp_path):
    target = tmp_path / "jsrc"

    assert settings.write_default_template(target) is True
    text = target.read_text(encoding="utf-8")
    template_lines = text.splitlines()
    template_keys = {
        line.split()[1]
        for line in template_lines
        if line.startswith("#set ")
    }
    env_keys = {
        line.removeprefix("# ").split(" -> ", 1)[0]
        for line in template_lines
        if line.startswith("# JS_") and " -> set " in line
    }

    assert {spec.key for spec in settings.REGISTRY} <= template_keys
    assert {spec.env for spec in settings.REGISTRY if spec.env} <= env_keys
    assert "minimal" in text
    assert "xhigh" in text
    assert "off disables" in text
    rendered = settings.collect_settings(config_paths=[target], env={})
    assert rendered["model"]["id"] == settings.DEFAULT_MODEL
    assert settings.write_default_template(target) is False
    assert target.read_text(encoding="utf-8") == text


def test_from_env_uses_platform_dirs_and_writes_global_jsrc(monkeypatch, tmp_path):
    config_home, data_home = _env_dirs(monkeypatch, tmp_path)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")

    cfg = from_env(save_session=True)

    assert cfg.provider_id == "deepseek"
    assert cfg.model == "deepseek-v4-flash"
    assert cfg.provider_api_key == "sk-test"
    assert cfg.reasoning_effort == "xhigh"
    assert cfg.sessions_dir == data_home / "js" / "sessions" / "defaultagent"
    assert (config_home / "js" / "jsrc").exists()
    assert not (tmp_path / "home" / ".js").exists()


def test_from_env_reads_global_jsrc(monkeypatch, tmp_path):
    config_home, _data_home = _env_dirs(monkeypatch, tmp_path)
    global_cfg = config_home / "js" / "jsrc"
    global_cfg.parent.mkdir(parents=True)
    global_cfg.write_text("set model.id global-model\nset limits.max_tool_iterations 8\n", encoding="utf-8")

    cfg = from_env(save_session=False)

    assert cfg.model == "global-model"
    assert cfg.max_tool_iterations == 8
    assert global_cfg.read_text(encoding="utf-8").startswith("set model.id global-model")


def test_presets_layer_over_base_in_order(monkeypatch, tmp_path):
    config_home, _data_home = _env_dirs(monkeypatch, tmp_path)
    js_dir = config_home / "js"
    js_dir.mkdir(parents=True)
    (js_dir / "jsrc").write_text("set model.id base-model\nset limits.fetch_timeout_s 5\n", encoding="utf-8")
    (js_dir / "jsrc.fast").write_text("set model.id fast-model\n", encoding="utf-8")
    (js_dir / "jsrc.slow").write_text("set model.id slow-model\nset limits.fetch_timeout_s 99\n", encoding="utf-8")

    # No preset: base wins.
    assert from_env(save_session=False).model == "base-model"

    # Last preset wins on overlap; earlier preset's non-overlapping keys persist.
    cfg = from_env(save_session=False, presets=["fast", "slow"])
    assert cfg.model == "slow-model"
    assert cfg.fetch_timeout_s == 99

    cfg2 = from_env(save_session=False, presets=["slow", "fast"])
    assert cfg2.model == "fast-model"
    assert cfg2.fetch_timeout_s == 99  # from slow; fast didn't set it


def test_allow_inline_code_default_is_on(monkeypatch, tmp_path):
    # Inverted: inline code runs by default, no flag/knob needed.
    _env_dirs(monkeypatch, tmp_path)
    monkeypatch.delenv("JS_ALLOW_INLINE_CODE", raising=False)
    cfg = from_env(save_session=False)
    assert cfg.allow_inline_code is True


def test_allow_inline_code_opt_out_via_jsrc(monkeypatch, tmp_path):
    # Deliberate opt-out through the config knob.
    config_home, _data_home = _env_dirs(monkeypatch, tmp_path)
    global_cfg = config_home / "js" / "jsrc"
    global_cfg.parent.mkdir(parents=True)
    global_cfg.write_text("set runtime.allow_inline_code off\n", encoding="utf-8")

    cfg = from_env(save_session=False)
    assert cfg.allow_inline_code is False


def test_allow_inline_code_opt_out_via_env(monkeypatch, tmp_path):
    # --im-a-pussy sets JS_ALLOW_INLINE_CODE=0; that env must flip the knob off
    # through the registry env layer.
    _env_dirs(monkeypatch, tmp_path)
    monkeypatch.setenv("JS_ALLOW_INLINE_CODE", "0")
    cfg = from_env(save_session=False)
    assert cfg.allow_inline_code is False


def test_project_config_env_and_cli_precedence(monkeypatch, tmp_path):
    _env_dirs(monkeypatch, tmp_path)
    project = tmp_path / "project"
    cfg_path = project / ".js" / "jsrc"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text("set model.id file-model\nset limits.max_tool_iterations 7\n", encoding="utf-8")
    monkeypatch.setenv("JS_MODEL", "env-model")

    cfg_env = from_env(cwd=project, save_session=False)
    assert cfg_env.model == "env-model"
    assert cfg_env.max_tool_iterations == 7

    cfg_cli = from_env(cwd=project, save_session=False, extras=["model.id=cli-model"])
    assert cfg_cli.model == "cli-model"


def test_js_model_provider_prefix_is_parsed_once(monkeypatch, tmp_path):
    _env_dirs(monkeypatch, tmp_path)
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


def test_provider_config_absent_when_unset(monkeypatch, tmp_path):
    captured_kwargs: dict = {}

    def stream_stub(**kwargs):
        captured_kwargs.update(kwargs)
        return _fake_stream_result("ok")

    monkeypatch.setattr("js.runtime.model_client.stream_model_async", stream_stub)
    cfg = _build_config(tmp_path, provider_id=None, provider_base_url=None, provider_api_key=None)
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

    assert captured_kwargs.get("provider_id") is None
    assert captured_kwargs.get("provider_base_url") is None
    assert captured_kwargs.get("provider_api_key") is None
