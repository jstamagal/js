from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import ai
import ai.types.messages
import ai.types.usage

from js import runtime, settings
from js.config import Config, from_env
from js.model_client import ModelStreamResult
from js.toolkit import ToolContext, build_default_registry


def _fake_stream_result(text: str = "ok") -> ModelStreamResult:
    return ModelStreamResult(
        text=text,
        tool_calls=[],
        reasoning="",
        usage=ai.types.usage.Usage(input_tokens=1, output_tokens=len(text)),
        finish_reason="stop",
        assistant_message=ai.types.messages.Message(
            role="assistant",
            parts=[ai.types.messages.TextPart(text=text)],
        ),
    )


def _config(
    tmp_path: Path,
    *,
    settings_view: dict | None = None,
) -> Config:
    return Config(
        agent_id="test-agent",
        agent_dir=tmp_path / "data" / "sessions" / "test-agent",
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
        history_file=tmp_path / "data" / "sessions" / "test-agent" / ".history",
        sessions_dir=tmp_path / "data" / "sessions" / "test-agent",
        session_file=tmp_path / "data" / "sessions" / "test-agent" / "runtime.jsonl",
        prompts_dir=tmp_path / "prompts",
        settings=settings_view or {},
    )


def _isolated_config_home(monkeypatch, tmp_path: Path) -> Path:
    config_home = tmp_path / "config"
    data_home = tmp_path / "data"
    project = tmp_path / "project"
    project.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.chdir(project)
    for spec in settings.REGISTRY:
        if spec.env:
            monkeypatch.delenv(spec.env, raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    return config_home / "js"


def test_from_env_carries_subagent_worker_limit(monkeypatch, tmp_path):
    config_dir = _isolated_config_home(monkeypatch, tmp_path)
    config_dir.mkdir(parents=True)
    (config_dir / "jsrc").write_text(
        "set model.id offline-test-model\nset limits.subagent_max_workers 3\n",
        encoding="utf-8",
    )

    cfg = from_env(save_session=False)

    assert cfg.subagent_max_workers == 3


def test_run_turn_copies_subagent_worker_limit_to_tool_context(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime.model_client, "stream_model_async", lambda **_kwargs: _fake_stream_result("ok"))
    cfg = replace(_config(tmp_path), subagent_max_workers=3)
    context = ToolContext(cwd=tmp_path)

    runtime.run_turn(
        cfg,
        "system",
        [{"role": "user", "content": "hi"}],
        runtime.Telemetry(None),
        trace_override=False,
        tool_registry=build_default_registry().select([]),
        tool_context=context,
        suppress_output=True,
    )

    assert context.subagent_max_workers == 3
