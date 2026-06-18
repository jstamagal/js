"""Tests for TOML settings, platform dirs, provider env, and runtime plumbing."""

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
    monkeypatch.delenv("JS_MODEL", raising=False)
    monkeypatch.delenv("JS_PROVIDER", raising=False)
    monkeypatch.delenv("JS_BASE_URL", raising=False)
    monkeypatch.delenv("JS_API_KEY", raising=False)
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


def test_load_toml_settings_returns_empty_dict_when_no_files(tmp_path):
    assert settings.load_toml_settings([tmp_path / "missing.toml"]) == {}


def test_load_toml_settings_merges_later_files_win(tmp_path):
    a = tmp_path / "a.toml"
    b = tmp_path / "b.toml"
    a.write_text("[model]\nid = \"first\"\nmax_output_tokens = 100\n", encoding="utf-8")
    b.write_text("[model]\nid = \"second\"\n", encoding="utf-8")
    merged = settings.load_toml_settings([a, b])
    assert merged["model"]["id"] == "second"
    assert merged["model"]["max_output_tokens"] == 100


def test_load_toml_settings_rejects_non_table_root(monkeypatch):
    import js.settings as _s

    monkeypatch.setattr(_s.tomllib, "load", lambda _fp: ["not", "a", "dict"])
    with pytest.raises(ValueError, match="top-level must be a TOML table"):
        _s.load_toml_settings([Path("/dev/null")])


def test_collect_settings_uses_built_in_default_when_no_file_or_env():
    out = settings.collect_settings(config_paths=[], env={})
    assert out["model"]["id"] == settings.DEFAULT_MODEL
    assert out["limits"]["max_tool_iterations"] == settings.DEFAULT_MAX_TOOL_ITERATIONS


def test_collect_settings_applies_env_after_file(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text("[model]\nid = \"file-model\"\n", encoding="utf-8")
    out = settings.collect_settings(config_paths=[cfg], env={"JS_MODEL": "env-model"})
    assert out["model"]["id"] == "env-model"


def test_collect_settings_cli_extras_win_over_env(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text("[model]\nid = \"file-model\"\n", encoding="utf-8")
    out = settings.collect_settings(config_paths=[cfg], env={"JS_MODEL": "env-model"}, extras=["model.id=cli-model"])
    assert out["model"]["id"] == "cli-model"


def test_write_default_template_creates_file_once(tmp_path):
    target = tmp_path / "config.toml"
    assert settings.write_default_template(target) is True
    text = target.read_text(encoding="utf-8")
    assert "Precedence, lowest to highest: built-in defaults < this file < project .js/config.toml" in text
    assert "[model]" in text and "[limits]" in text and "[provider]" in text
    assert "JS_MODEL" in text and "JS_PROVIDER" in text and "JS_BASE_URL" in text and "JS_API_KEY" in text
    assert "JS_PROVIDER_ID" not in text
    assert "JS_PROVIDER_BASE_URL" not in text
    assert "JS_PROVIDER_API_KEY" not in text
    assert "JS_JSONL_MAX_LINE_CHARS" in text
    assert settings.write_default_template(target) is False


def test_from_env_uses_platform_dirs_and_does_not_create_home_dot_js(monkeypatch, tmp_path):
    config_home, data_home = _env_dirs(monkeypatch, tmp_path)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")

    cfg = from_env(save_session=True)

    assert cfg.provider_id == "deepseek"
    assert cfg.model == "deepseek-v4-flash"
    assert cfg.provider_api_key == "sk-test"
    assert cfg.reasoning_effort == "xhigh"
    assert cfg.sessions_dir == data_home / "js" / "sessions" / "defaultagent"
    assert (config_home / "js" / "config.toml").exists()
    assert not (tmp_path / "home" / ".js").exists()


def test_project_config_env_and_cli_precedence(monkeypatch, tmp_path):
    _env_dirs(monkeypatch, tmp_path)
    project = tmp_path / "project"
    cfg_path = project / ".js" / "config.toml"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text("[model]\nid = \"file-model\"\n[limits]\nmax_tool_iterations = 7\n", encoding="utf-8")
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

    monkeypatch.setattr("js.runtime.model_client.stream_model", stream_stub)
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

    monkeypatch.setattr("js.runtime.model_client.stream_model", stream_stub)
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
