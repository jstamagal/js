"""Effective `/set` view + `/save` round-trip.

The settings store lies: a `--model` flag, a saved-login base URL, and env-read
sampling (JS_TEMPERATURE ...) feed the next turn without ever landing in the store.
`/set` overlays those live sources; `/save` folds them back into a jsrc so a
reload reproduces the running configuration.
"""

from __future__ import annotations

import copy
from dataclasses import replace

from js import cli, setcmd, settings
from js.config import Config
from js.sampling import Sampling
from js import messages as msgs


# ---------------------------------------------------------------------------
# /set and /show: display forms vs mutations
# ---------------------------------------------------------------------------

def _repl_cfg(tmp_path) -> Config:
    d = tmp_path / "sessions"
    return Config(
        agent_id="a", agent_dir=d, model="offline-test-model", provider_id=None,
        provider_base_url=None, provider_api_key=None, reasoning_effort=None,
        max_output_tokens=None, max_tool_iterations=5, max_bash_output_bytes=65536,
        max_tool_result_bytes=65536, fetch_timeout_s=5, debug_log=None, trace=False,
        history_file=d / ".history", sessions_dir=d, session_file=d / "s.jsonl",
        prompts_dir=tmp_path / "prompts",
    )


def test_set_display_forms_leave_the_store_alone_and_mutations_reach_live_state(tmp_path):
    cfg = _repl_cfg(tmp_path)
    state = {"messages": [], "system": "sys", "settings": settings.seed_defaults()}
    for line in ("/set", "/show", "/set MODEL", "/show PROVIDER"):
        before = copy.deepcopy(state["settings"])
        assert cli._handle_command(line, state, cfg) is True
        assert state["settings"] == before

    cli._handle_command("/set TEMPERATURE 0.3", state, cfg)
    assert settings.get_dotted(state["settings"], ("TEMPERATURE",)) == 0.3
    assert state["live_set"]["TEMPERATURE"] == 0.3

    cli._handle_command("/set -TEMPERATURE", state, cfg)
    assert settings.get_dotted(state["settings"], ("TEMPERATURE",)) is None
    assert "TEMPERATURE" not in state["live_set"]


# ---------------------------------------------------------------------------
# Effective view rendering
# ---------------------------------------------------------------------------

def test_show_lines_effective_annotates_and_masks():
    store = settings.seed_defaults()
    settings.set_dotted(store, ("MODEL",), "stored/model")
    settings.set_dotted(store, ("API_KEY",), "private-store-key")
    settings.set_dotted(store, ("PROVIDER",), "stored-provider")
    settings.set_dotted(store, ("PARANOID_MODE",), "on")
    overlay = {"MODEL": setcmd.LiveValue("live/model", "--model flag")}

    one = setcmd.show_lines_effective(store, overlay, "MODEL")
    rendered = "\n".join(one.lines)
    assert one.error is None
    assert "live/model" in rendered
    assert "--model flag" in rendered
    assert "stored/model" not in rendered

    full = setcmd.show_lines_effective(store, overlay)
    rendered = "\n".join(full.lines)
    assert "live/model" in rendered
    assert "--model flag" in rendered
    assert "API_KEY" in rendered
    assert "private-store-key" not in rendered
    assert "stored/model" not in rendered

    overlay["API_KEY"] = setcmd.LiveValue("masked-login-fixture", "saved-login-fixture")
    secret = setcmd.show_lines_effective(store, overlay, "API_KEY")
    displayed = "\n".join(secret.lines)
    assert "masked-login-fixture" in displayed
    assert "saved-login-fixture" in displayed
    assert "private-store-key" not in displayed

    # Values without an overlay still reflect the explicitly supplied store.
    plain = setcmd.show_lines_effective(store, overlay, "PROVIDER")
    assert "stored-provider" in "\n".join(plain.lines)


def test_show_lines_effective_unknown_key_errors():
    result = setcmd.show_lines_effective(settings.seed_defaults(), {}, "nope.nope")
    assert result.error == msgs.UNKNOWN_SETTING.text(key="nope.nope")


# ---------------------------------------------------------------------------
# Overlay computation (the live route diffed against the store)
# ---------------------------------------------------------------------------


def test_overlay_login_provider_and_masked_key():
    class _Login:
        provider_base_url = "http://localhost:8050/v1"
        provider_api_key = "sk-login"

    store = settings.seed_defaults()
    settings.set_dotted(store, ("PARANOID_MODE",), "on")
    overlay = cli._live_settings_overlay(
        store,
        live_model=None,
        model_source=None,
        provider_id="testes",
        provider_base_url="http://localhost:8050/v1",
        provider_api_key="sk-login",
        saved_login=_Login(),
        provider_default_base_url=None,
        eff_sampling=Sampling(),
        env_sampling=Sampling(),
        manifest_sampling=Sampling(),
        environ={},
    )
    assert overlay["PROVIDER"].source == "login testes"
    assert overlay["BASE_URL"] == setcmd.LiveValue(
        "http://localhost:8050/v1", "login testes"
    )
    # secret is never printed in the clear
    assert overlay["API_KEY"].display
    assert "sk-login" not in overlay["API_KEY"].display
    assert overlay["API_KEY"].source == "login testes"


# ---------------------------------------------------------------------------
# /save writer
# ---------------------------------------------------------------------------

def _effective_store() -> dict:
    store = settings.seed_defaults()
    settings.set_dotted(store, ("MODEL",), "testes/test")
    settings.set_dotted(store, ("PROVIDER",), "deepseek")
    settings.set_dotted(store, ("BASE_URL",), "http://localhost:8050/v1")
    settings.set_dotted(store, ("API_KEY",), "sk-secret")
    settings.set_dotted(store, ("TEMPERATURE",), 1.0)
    return store


def test_save_round_trip_reproduces_effective_values(tmp_path):
    store = _effective_store()
    path = tmp_path / "jsrc"

    settings.save_settings_to_jsrc(path, store)

    reloaded = settings.collect_settings(config_paths=[path], env={})
    for key in (
        "MODEL",
        "PROVIDER",
        "BASE_URL",
        "API_KEY",
        "TEMPERATURE",
    ):
        spec = settings.SPEC_BY_KEY[key]
        assert settings.get_dotted(reloaded, spec.path) == settings.get_dotted(store, spec.path)


def test_save_after_logout_keeps_provider_settings_cleared(tmp_path):
    store = settings.seed_defaults()
    store.update(PROVIDER="ollama", BASE_URL="http://offline.invalid/v1", API_KEY="old-key")
    cfg = replace(_repl_cfg(tmp_path), settings=store, provider_id="ollama",
                  provider_base_url=store["BASE_URL"], provider_api_key=store["API_KEY"])
    state = {"settings": copy.deepcopy(store), "model": cfg.model, "messages": []}

    assert cli._handle_command("/logout", state, cfg)
    assert cli._cmd_save("", state, cfg) is None
    reloaded = settings.collect_settings(config_paths=[cli._paths.global_config_file()], env={})

    assert all(settings.knob(reloaded, key) is None for key in ("PROVIDER", "BASE_URL", "API_KEY"))
