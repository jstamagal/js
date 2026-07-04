from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import ai
import ai.types.messages
import ai.types.usage

import js.toolkit.artifact as artifact_module
from js import runtime, settings
from js.config import Config, from_env
from js.model_client import ModelStreamResult
from js.toolkit import ToolContext, build_default_registry
from js.toolkit.artifact import _artifact_bin, _artifact_dir, _base_url
from js.toolkit.wiki.helpers import infer_vault, resolve_vault


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
    artifact_dir: str | None = None,
    artifact_url: str | None = None,
    artifact_bin: str | None = None,
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
        artifact_dir=artifact_dir,
        artifact_url=artifact_url,
        artifact_bin=artifact_bin,
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


def test_artifact_registry_defaults_are_none_and_not_seeded():
    defaults = {
        spec.key: spec.default
        for spec in settings.REGISTRY
        if spec.key in {"artifact.dir", "artifact.url", "artifact.bin"}
    }

    assert defaults == {"artifact.dir": None, "artifact.url": None, "artifact.bin": None}
    assert "artifact" not in settings.seed_defaults()


def test_artifact_helpers_prefer_context_then_env_then_builtin(monkeypatch, tmp_path):
    monkeypatch.setattr(artifact_module, "ARTIFACT_DIR", Path("/srv/artifacts"))
    monkeypatch.setattr(artifact_module, "BASE_URL", "http://localhost")
    monkeypatch.setattr(artifact_module, "ARTIFACT_BIN", "artifact")

    env_dir = tmp_path / "env-artifacts"
    ctx_dir = tmp_path / "ctx-artifacts"
    monkeypatch.setenv("ARTIFACT_DIR", str(env_dir))
    monkeypatch.setenv("ARTIFACT_URL", "http://env.local/")
    monkeypatch.setenv("ARTIFACT_BIN", "artifact-env")

    ctx = ToolContext(
        cwd=tmp_path,
        artifact_dir=ctx_dir,
        artifact_url="http://ctx.local/",
        artifact_bin="artifact-ctx",
    )
    assert _artifact_dir(ctx) == ctx_dir
    assert _base_url(ctx) == "http://ctx.local"
    assert _artifact_bin(ctx) == "artifact-ctx"

    unset_ctx = ToolContext(cwd=tmp_path)
    assert _artifact_dir(unset_ctx) == env_dir
    assert _artifact_dir(None) == env_dir
    assert _base_url(unset_ctx) == "http://env.local"
    assert _base_url(None) == "http://env.local"
    assert _artifact_bin(unset_ctx) == "artifact-env"
    assert _artifact_bin(None) == "artifact-env"

    monkeypatch.delenv("ARTIFACT_DIR")
    monkeypatch.delenv("ARTIFACT_URL")
    monkeypatch.delenv("ARTIFACT_BIN")
    assert _artifact_dir(ToolContext(cwd=tmp_path)) == Path("/srv/artifacts")
    assert _base_url(ToolContext(cwd=tmp_path)) == "http://localhost"
    assert _artifact_bin(ToolContext(cwd=tmp_path)) == "artifact"


def test_from_env_carries_configured_artifact_settings(monkeypatch, tmp_path):
    config_dir = _isolated_config_home(monkeypatch, tmp_path)
    config_dir.mkdir(parents=True)
    (config_dir / "jsrc").write_text(
        "set artifact.dir /tmp/x\n"
        "set artifact.url http://artifact.test/\n"
        "set artifact.bin artifact-test\n",
        encoding="utf-8",
    )

    cfg = from_env(save_session=False)

    assert cfg.artifact_dir == "/tmp/x"
    assert cfg.artifact_url == "http://artifact.test/"
    assert cfg.artifact_bin == "artifact-test"


def test_from_env_leaves_artifact_settings_none_without_config_lines(monkeypatch, tmp_path):
    config_dir = _isolated_config_home(monkeypatch, tmp_path)
    config_dir.mkdir(parents=True)
    (config_dir / "jsrc").write_text("set model.id offline-test-model\n", encoding="utf-8")

    cfg = from_env(save_session=False)

    assert cfg.artifact_dir is None
    assert cfg.artifact_url is None
    assert cfg.artifact_bin is None


def test_from_env_carries_subagent_worker_limit(monkeypatch, tmp_path):
    config_dir = _isolated_config_home(monkeypatch, tmp_path)
    config_dir.mkdir(parents=True)
    (config_dir / "jsrc").write_text(
        "set model.id offline-test-model\nset limits.subagent_max_workers 3\n",
        encoding="utf-8",
    )

    cfg = from_env(save_session=False)

    assert cfg.subagent_max_workers == 3


def test_run_turn_copies_artifact_config_and_vault_aliases_to_tool_context(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime.model_client, "stream_model_async", lambda **_kwargs: _fake_stream_result("ok"))
    cfg = replace(
        _config(
            tmp_path,
            artifact_dir="/cfg/artifacts",
            artifact_url="http://cfg.local/",
            artifact_bin="artifact-cfg",
            settings_view={"wiki": {"aliases": {"creative": "/p"}}},
        ),
        subagent_max_workers=3,
    )
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

    assert context.artifact_dir == "/cfg/artifacts"
    assert context.artifact_url == "http://cfg.local/"
    assert context.artifact_bin == "artifact-cfg"
    assert context.subagent_max_workers == 3
    assert context.vault_aliases == {"creative": "/p"}


def test_resolve_vault_uses_context_aliases_and_falls_through_to_paths(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    context = ToolContext(cwd=tmp_path, vault_aliases={"creative": "~/wiki-creative"})

    assert resolve_vault("creative", context) == (home / "wiki-creative").resolve()
    assert resolve_vault("missing", context) == (tmp_path / "missing").resolve()


def test_infer_vault_fails_closed_and_detects_sentinel_or_wiki_dir(tmp_path):
    non_vault = tmp_path / "plain"
    non_vault.mkdir()
    assert infer_vault(None, non_vault) is None

    sentinel_vault = tmp_path / "notes"
    sentinel_vault.mkdir()
    (sentinel_vault / "PURPOSE.md").write_text("purpose\n", encoding="utf-8")
    assert infer_vault(None, sentinel_vault / "nested") == str(sentinel_vault.resolve())

    named_vault = tmp_path / "wiki-general"
    named_vault.mkdir()
    assert infer_vault(None, named_vault) == str(named_vault.resolve())
