"""REPL /compact-auto toggle and the /login <name> [key] [url] [provider] builder."""

from __future__ import annotations

from pathlib import Path

import ai
from repl_driver import record_turns, run_async

from js import cli, logins, providers, repl_state, settings
from js.config import Config
from js.memory import append_message


def make_cfg(tmp_path: Path) -> Config:
    d = tmp_path / ".js" / "sessions" / "a"
    return Config(
        agent_id="a",
        agent_dir=d,
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
        history_file=d / ".history",
        sessions_dir=d,
        session_file=d / "s.jsonl",
        prompts_dir=tmp_path / "prompts" / "a",
    )


def repl_cfg(tmp_path: Path, monkeypatch) -> Config:
    """make_cfg with the agent's prompt in place, the working directory the
    test's own."""
    monkeypatch.chdir(tmp_path)
    cfg = make_cfg(tmp_path)
    cfg.prompts_dir.mkdir(parents=True)
    (cfg.prompts_dir / "01-prompt.md").write_text("sys\n", encoding="utf-8")
    return cfg


def _compaction_auto(turn) -> bool:
    return settings.get_dotted(turn.settings, ("COMPACTION_AUTO",))


# ---- /compact-auto ----

def test_compact_auto_toggles_setting_and_does_not_compact(tmp_path, monkeypatch):
    cfg = repl_cfg(tmp_path, monkeypatch)
    compacted: list[int] = []
    monkeypatch.setattr(cli.compaction, "compact_now_sync", lambda *a, **k: compacted.append(1) or "nope")
    turns = record_turns(monkeypatch)

    run_async(monkeypatch, cfg, ["/compact-auto off", "one", "/compact-auto on", "two"], wait=True,
              live_settings=settings.seed_defaults())

    assert [_compaction_auto(turn) for turn in turns] == [False, True]
    assert compacted == []  # toggling must NOT trigger a compaction


def test_compact_auto_does_not_swallow_plain_compact(tmp_path, monkeypatch):
    cfg = repl_cfg(tmp_path, monkeypatch)
    seen_focus: list[str] = []
    monkeypatch.setattr(
        cli.compaction, "compact_now_sync",
        lambda cfg, system, messages, *, model=None, focus="", forced=False, **_: seen_focus.append(focus) or "ok",
    )
    turns = record_turns(monkeypatch)

    # plain /compact still runs a compaction with a clean focus (not "-auto on")
    run_async(monkeypatch, cfg, ["/compact please"], live_settings=settings.seed_defaults())

    assert turns == []
    assert seen_focus == ["please"]


def test_compact_command_model_flag_picks_the_summarizer(tmp_path, monkeypatch):
    cfg = repl_cfg(tmp_path, monkeypatch)
    seen: list[tuple[str | None, str, bool]] = []

    def compact_stub(compact_cfg, system, messages, *, model=None, focus="", forced=False, **_):
        seen.append((model, focus, forced))
        return "ok"

    monkeypatch.setattr(cli.compaction, "compact_now_sync", compact_stub)
    turns = record_turns(monkeypatch)

    run_async(monkeypatch, cfg, [
        "/compact -m fast-summarizer keep the API decisions",
        "/compact --model fast-summarizer",
        "/compact -m fast-summarizer up to here",
        # no flag = no override; COMPACTION_EXTERNAL_MODEL (or the active model) decides
        "/compact now",
    ], live_settings=settings.seed_defaults())

    assert turns == []
    assert seen == [
        ("fast-summarizer", "keep the API decisions", False),
        ("fast-summarizer", "", False),
        ("fast-summarizer", "", True),
        (None, "now", False),
    ]


def test_compact_command_model_flag_without_value_prints_usage(tmp_path, monkeypatch, capsys):
    cfg = repl_cfg(tmp_path, monkeypatch)
    compacted: list[int] = []
    monkeypatch.setattr(cli.compaction, "compact_now_sync", lambda *a, **k: compacted.append(1) or "ok")
    turns = record_turns(monkeypatch)

    run_async(monkeypatch, cfg, ["/compact -m"], live_settings=settings.seed_defaults())

    assert turns == []
    assert capsys.readouterr().out
    assert compacted == []


def test_compact_command_uses_live_compact_settings(tmp_path, monkeypatch):
    cfg = repl_cfg(tmp_path, monkeypatch)
    seen_models: list[str | None] = []

    def compact_stub(compact_cfg, system, messages, *, model=None, focus="", forced=False, **_):
        seen_models.append(settings.get_dotted(compact_cfg.settings, ("COMPACTION_EXTERNAL_MODEL",)))
        return "ok"

    monkeypatch.setattr(cli.compaction, "compact_now_sync", compact_stub)
    turns = record_turns(monkeypatch)

    run_async(monkeypatch, cfg, ["/set COMPACTION_EXTERNAL_MODEL compact-live-model", "/compact"],
              live_settings=settings.seed_defaults())

    assert turns == []
    assert seen_models == ["compact-live-model"]


def test_compact_command_applies_live_provider_extra_to_summary_model(tmp_path, monkeypatch):
    cfg = repl_cfg(tmp_path, monkeypatch)
    append_message(cfg.session_file, {"role": "user", "content": "old context " * 7000})
    append_message(cfg.session_file, {"role": "assistant", "content": "latest answer"})
    params_seen: list[dict] = []

    class FakeProvider:
        async def aclose(self):
            return None

    class FakeModel:
        provider = FakeProvider()

    async def stream_async_stub(*, model, messages, tools, params, on_text):
        params_seen.append(params)
        return cli.runtime.model_client.ModelStreamResult(
            text="Summary",
            tool_calls=[],
            reasoning="",
            usage=None,
            finish_reason="stop",
            assistant_message=ai.assistant_message("Summary"),
        )

    monkeypatch.setattr(cli.runtime.model_client, "resolve_model", lambda *args, **kwargs: FakeModel())
    monkeypatch.setattr(cli.runtime.model_client, "_stream_async", stream_async_stub)
    turns = record_turns(monkeypatch)

    run_async(monkeypatch, cfg, ['/set EXTRA_PARAMS {"extra_body":{"compact_flag":true}}', "/compact up to here"],
              live_settings=settings.seed_defaults())

    assert turns == []
    assert params_seen[0].extra_body == {"compact_flag": True}


def test_compact_auto_bad_arg_usage(tmp_path, monkeypatch, capsys):
    cfg = repl_cfg(tmp_path, monkeypatch)
    turns = record_turns(monkeypatch)

    run_async(monkeypatch, cfg, ["/compact-auto maybe", "hello"], wait=True,
              live_settings=settings.seed_defaults())  # handled, prints usage

    assert capsys.readouterr().out
    # unchanged
    [turn] = turns
    assert _compaction_auto(turn) is True


# ---- /login <name> [key] [url] [provider] ----

def test_login_inline_creds_builds_and_saves(tmp_path, monkeypatch):
    cfg = repl_cfg(tmp_path, monkeypatch)
    saved: list = []
    monkeypatch.setattr(logins, "save_login", lambda login: saved.append(login))
    monkeypatch.setattr(repl_state, "apply_saved_login_to_state",
                        lambda state, name: setattr(state, "provider_id", name) is None)
    turns = record_turns(monkeypatch)

    run_async(monkeypatch, cfg, ["/login fartbox xkey http://127.0.0.1:8020/v1 openai"])

    assert turns == []
    assert len(saved) == 1
    lg = saved[0]
    assert lg.provider_id == "fartbox"
    assert lg.provider_api_key == "xkey"
    assert lg.provider_base_url == "http://127.0.0.1:8020/v1"
    assert lg.sdk_provider_id == providers.get_provider("openai").effective_sdk_provider_id


def test_login_known_provider_infers_type(tmp_path, monkeypatch):
    cfg = repl_cfg(tmp_path, monkeypatch)
    saved: list = []
    monkeypatch.setattr(logins, "save_login", lambda login: saved.append(login))
    monkeypatch.setattr(repl_state, "apply_saved_login_to_state", lambda state, name: True)
    turns = record_turns(monkeypatch)

    run_async(monkeypatch, cfg, ["/login deepseek mykey"])

    assert turns == []
    assert len(saved) == 1
    assert saved[0].provider_api_key == "mykey"
    assert saved[0].sdk_provider_id == providers.get_provider("deepseek").effective_sdk_provider_id


def test_login_custom_name_without_type_errors(tmp_path, monkeypatch, capsys):
    cfg = repl_cfg(tmp_path, monkeypatch)
    saved: list = []
    monkeypatch.setattr(logins, "save_login", lambda login: saved.append(login))
    turns = record_turns(monkeypatch)

    run_async(monkeypatch, cfg, ["/login fartbox xkey"])  # key given, no provider type

    assert turns == []
    assert saved == []  # nothing saved
    assert capsys.readouterr().out


def test_login_bare_name_loads_saved(tmp_path, monkeypatch):
    cfg = repl_cfg(tmp_path, monkeypatch)
    loaded: list[str] = []
    monkeypatch.setattr(repl_state, "set_provider_state", lambda state, name: loaded.append(name))
    turns = record_turns(monkeypatch)

    run_async(monkeypatch, cfg, ["/login deepseek"])

    assert turns == []
    assert loaded == ["deepseek"]  # bare /login keeps the old load-saved behavior
