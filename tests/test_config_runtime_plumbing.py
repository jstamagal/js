from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import ai
import ai.types.messages
import ai.types.usage

from js import cli, runtime, settings
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
    return tmp_path / "home" / ".js"


def test_from_env_carries_subagent_worker_limit(monkeypatch, tmp_path):
    config_dir = _isolated_config_home(monkeypatch, tmp_path)
    config_dir.mkdir(parents=True)
    (config_dir / "jsrc").write_text(
        "set MODEL offline-test-model\nset SUBAGENT_WORKERS_MAX 3\n",
        encoding="utf-8",
    )

    cfg = from_env(save_session=False)

    assert cfg.subagent_max_workers == 3


def test_config_preserves_positional_constructor(tmp_path):
    cfg = Config(
        "test-agent", tmp_path / "agent", "offline-test-model", "test-provider",
        "http://localhost", "test-key", "low", tmp_path / "debug.log",
        tmp_path / "history", tmp_path / "sessions", tmp_path / "session.jsonl",
        tmp_path / "prompts",
    )

    assert cfg == Config(
        agent_id="test-agent", agent_dir=tmp_path / "agent", model="offline-test-model",
        provider_id="test-provider", provider_base_url="http://localhost",
        provider_api_key="test-key", reasoning_effort="low", debug_log=tmp_path / "debug.log",
        history_file=tmp_path / "history", sessions_dir=tmp_path / "sessions",
        session_file=tmp_path / "session.jsonl", prompts_dir=tmp_path / "prompts",
    )


def test_shell_env_allow_defaults_to_existing_safe_set_and_loads_from_jsrc(
    monkeypatch, tmp_path
):
    assert tuple(settings.default_value("SHELL_ENV_KEEP")) == (
        "PATH",
        "HOME",
        "USER",
        "LANG",
        "LC_ALL",
        "TERM",
        "PWD",
        "SHELL",
        "BEADS_ACTOR",
    )
    config_dir = _isolated_config_home(monkeypatch, tmp_path)
    config_dir.mkdir(parents=True)
    (config_dir / "jsrc").write_text(
        'set MODEL offline-test-model\n'
        'set SHELL_ENV_KEEP ["PATH","FORGECODE_TOKEN"]\n',
        encoding="utf-8",
    )

    cfg = from_env(save_session=False)

    assert cfg.shell_env_allow == ("PATH", "FORGECODE_TOKEN")


def test_shell_env_allow_is_live_and_installed_on_the_tool_context(monkeypatch, tmp_path):
    monkeypatch.setattr(
        runtime.model_client,
        "stream_model_async",
        lambda **_kwargs: _fake_stream_result("ok"),
    )
    cfg = replace(_config(tmp_path), shell_env_allow=("PATH",))
    live_settings = settings.seed_defaults()
    settings.set_dotted(
        live_settings,
        ("SHELL_ENV_KEEP",),
        ["PATH", "FORGECODE_TOKEN"],
    )
    live_cfg = cli._cfg_for_live_state(cfg, {"settings": live_settings})
    context = ToolContext(cwd=tmp_path)

    runtime.run_turn(
        live_cfg,
        "system",
        [{"role": "user", "content": "hi"}],
        runtime.Telemetry(None),
        trace_override=False,
        tool_registry=build_default_registry().select([]),
        tool_context=context,
        suppress_output=True,
    )

    assert live_cfg.shell_env_allow == ("PATH", "FORGECODE_TOKEN")
    assert context.shell_env_allow == ("PATH", "FORGECODE_TOKEN")


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


def test_a_config_built_in_code_runs_a_turn_and_a_compaction_with_an_empty_home(
        monkeypatch, tmp_path):
    import asyncio

    import ai.models
    import ai.types.events

    from js import compaction, model_client

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    requests = []

    def open_stream(**kwargs):
        requests.append(kwargs["model"])

        async def generate():
            yield ai.types.events.StreamStart()
            yield ai.types.events.TextStart(block_id="t")
            yield ai.types.events.TextDelta(chunk="summary", block_id="t")
            yield ai.types.events.TextEnd(block_id="t")
            yield ai.types.events.StreamEnd()

        return ai.models.Stream(generate())

    monkeypatch.setattr(model_client, "_open_stream", open_stream)
    cfg = Config(model="deepseek-chat", provider_id="deepseek", provider_api_key="test-key",
                 settings={"COMPACTION_KEEP_RECENT_TOKENS": 1})

    messages = [{"role": "user", "content": "old content " * 100}]
    asyncio.run(runtime.run_turn_async(cfg, "SYSTEM", messages, runtime.Telemetry(None),
                                       suppress_output=True))
    messages.append({"role": "user", "content": "new content"})
    result = compaction.compact_now_sync(cfg, "SYSTEM", messages, forced=True)

    assert compaction.compacted(result)
    assert [model.id for model in requests] == ["deepseek-chat", "deepseek-chat"]
    assert list(home.rglob("*")) == []
