"""REPL /compact-auto toggle and the /login <name> [key] [url] [provider] builder."""

from __future__ import annotations

from pathlib import Path

from js import cli, providers, settings
from js.config import Config


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


# ---- /compact-auto ----

def test_compact_auto_toggles_setting_and_does_not_compact(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    state = {"messages": [], "system": "sys", "settings": settings.seed_defaults()}
    compacted: list[int] = []
    monkeypatch.setattr(cli.runtime, "compact_messages", lambda *a, **k: compacted.append(1) or "nope")

    assert cli._handle_command("/compact-auto off", state, cfg) is True
    assert settings.get_dotted(state["settings"], ("compact", "auto")) is False
    assert compacted == []  # toggling must NOT trigger a compaction

    assert cli._handle_command("/compact-auto on", state, cfg) is True
    assert settings.get_dotted(state["settings"], ("compact", "auto")) is True
    assert compacted == []


def test_compact_auto_does_not_swallow_plain_compact(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    state = {"messages": [], "system": "sys", "settings": settings.seed_defaults()}
    seen_focus: list[str] = []
    monkeypatch.setattr(cli, "_cfg_for_active_model", lambda cfg, state: cfg)
    monkeypatch.setattr(
        cli.runtime, "compact_messages",
        lambda cfg, system, messages, *, focus="", forced=False: seen_focus.append(focus) or "ok",
    )
    # plain /compact still runs a compaction with a clean focus (not "-auto on")
    assert cli._handle_command("/compact please", state, cfg) is True
    assert seen_focus == ["please"]


def test_compact_auto_bad_arg_usage(tmp_path):
    cfg = make_cfg(tmp_path)
    state = {"messages": [], "system": "sys", "settings": settings.seed_defaults()}
    assert cli._handle_command("/compact-auto maybe", state, cfg) is True  # handled, prints usage
    # unchanged
    assert settings.get_dotted(state["settings"], ("compact", "auto")) is True


# ---- /login <name> [key] [url] [provider] ----

def test_login_inline_creds_builds_and_saves(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    saved: list = []
    monkeypatch.setattr(cli.logins, "save_login", lambda login: saved.append(login))
    monkeypatch.setattr(cli, "_apply_saved_login_to_state",
                        lambda state, name: state.update(provider_id=name) is None)
    state = {"messages": []}

    assert cli._handle_command("/login fartbox xkey http://127.0.0.1:8020/v1 openai", state, cfg) is True
    assert len(saved) == 1
    lg = saved[0]
    assert lg.provider_id == "fartbox"
    assert lg.provider_api_key == "xkey"
    assert lg.provider_base_url == "http://127.0.0.1:8020/v1"
    assert lg.sdk_provider_id == providers.get_provider("openai").effective_sdk_provider_id


def test_login_known_provider_infers_type(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    saved: list = []
    monkeypatch.setattr(cli.logins, "save_login", lambda login: saved.append(login))
    monkeypatch.setattr(cli, "_apply_saved_login_to_state", lambda state, name: True)
    state = {"messages": []}

    assert cli._handle_command("/login deepseek mykey", state, cfg) is True
    assert len(saved) == 1
    assert saved[0].provider_api_key == "mykey"
    assert saved[0].sdk_provider_id == providers.get_provider("deepseek").effective_sdk_provider_id


def test_login_custom_name_without_type_errors(tmp_path, monkeypatch, capsys):
    cfg = make_cfg(tmp_path)
    saved: list = []
    monkeypatch.setattr(cli.logins, "save_login", lambda login: saved.append(login))
    state = {"messages": []}

    assert cli._handle_command("/login fartbox xkey", state, cfg) is True  # key given, no provider type
    assert saved == []  # nothing saved
    assert "not a known provider" in capsys.readouterr().out


def test_login_bare_name_loads_saved(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    loaded: list[str] = []
    monkeypatch.setattr(cli, "_set_provider_state", lambda state, name: loaded.append(name))
    state = {"messages": []}

    assert cli._handle_command("/login deepseek", state, cfg) is True
    assert loaded == ["deepseek"]  # bare /login keeps the old load-saved behavior
