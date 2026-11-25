"""Effective `/set` view + `/save` round-trip.

The settings store lies: a `--model` flag, a saved-login base URL, and env-read
sampling (JS_TEMPERATURE ...) feed the next turn without ever landing in the store.
`/set` overlays those live sources; `/save` folds them back into a jsrc so a
reload reproduces the running configuration.
"""

from __future__ import annotations

import asyncio
import copy
from dataclasses import replace

import pytest
from repl_driver import record_turns, run_async, within

from js import (
    jsrc_save,
    messages as msgs,
    paths,
    repl_settings_commands,
    runtime,
    setcmd,
    setting_layers,
    settings,
)
from js.config import Config
from js.sampling import Sampling

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


def _with_prompt(tmp_path, monkeypatch, cfg: Config) -> Config:
    """`cfg` with its agent prompt in place, the working directory the test's own."""
    monkeypatch.chdir(tmp_path)
    cfg.prompts_dir.mkdir(parents=True)
    (cfg.prompts_dir / "01-prompt.md").write_text("sys\n", encoding="utf-8")
    return cfg


def test_set_display_forms_leave_the_store_alone_and_mutations_reach_live_state(tmp_path, monkeypatch):
    cfg = _with_prompt(tmp_path, monkeypatch, _repl_cfg(tmp_path))
    turns = record_turns(monkeypatch)

    run_async(monkeypatch, cfg, [
        "before",
        "/set", "/show", "/set MODEL", "/show PROVIDER",
        "displayed",
        "/set TEMPERATURE 0.3",
        "set",
        "/set -TEMPERATURE",
        "cleared",
    ], wait=True, live_settings=settings.seed_defaults())

    before, displayed, set_, cleared = turns
    assert displayed.settings == before.settings

    assert settings.get_dotted(set_.settings, ("TEMPERATURE",)) == 0.3
    assert set_.cfg.layer_set["TEMPERATURE"] == 0.3

    assert settings.get_dotted(cleared.settings, ("TEMPERATURE",)) is None
    assert "TEMPERATURE" not in cleared.cfg.layer_set


@pytest.mark.parametrize("steer_mode", ["now", "batch", "one"])
def test_a_turn_runs_on_the_live_settings_of_its_start(tmp_path, monkeypatch, steer_mode):
    """A prompt typed while a turn runs waits for it; a /set typed after that
    prompt, before its turn starts, applies to its turn and not to the one
    already running."""
    cfg = _with_prompt(tmp_path, monkeypatch, _repl_cfg(tmp_path))
    started, release = asyncio.Event(), asyncio.Event()
    turns: list[tuple[str, object]] = []

    async def turn(cfg, system, messages, *args, **kwargs):
        turns.append((messages[-1]["content"], settings.get_dotted(cfg.settings, ("TEMPERATURE",))))
        if not started.is_set():
            started.set()
            await release.wait()

    async def typing(enter):
        await enter(f"/set STEER_MODE {steer_mode}")
        await enter("first")
        await within(started.wait(), "the first turn to start")
        await enter("second")
        await enter("/set TEMPERATURE 0.3")
        release.set()

    monkeypatch.setattr(runtime, "run_turn_async", turn)
    run_async(monkeypatch, cfg, typing, live_settings=settings.seed_defaults())

    assert turns == [("first", None), ("second", 0.3)]


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
    overlay = repl_settings_commands._live_settings_overlay(
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


def test_save_round_trip_reproduces_the_values_it_is_given(tmp_path):
    store = _effective_store()
    path = tmp_path / "jsrc"

    jsrc_save.save_settings_to_jsrc(path, store, keys=["MODEL", "PROVIDER", "BASE_URL", "API_KEY", "TEMPERATURE"])

    reloaded = setting_layers.collect_settings(config_paths=[path], env={})
    for key in (
        "MODEL",
        "PROVIDER",
        "BASE_URL",
        "API_KEY",
        "TEMPERATURE",
    ):
        spec = settings.SPEC_BY_KEY[key]
        assert settings.get_dotted(reloaded, spec.path) == settings.get_dotted(store, spec.path)


def test_save_after_logout_keeps_provider_settings_cleared(tmp_path, monkeypatch):
    store = settings.seed_defaults()
    store.update(PROVIDER="ollama", BASE_URL="http://offline.invalid/v1", API_KEY="old-key")
    cfg = replace(_repl_cfg(tmp_path), settings=store, provider_id="ollama",
                  provider_base_url=store["BASE_URL"], provider_api_key=store["API_KEY"])
    cfg = _with_prompt(tmp_path, monkeypatch, cfg)
    turns = record_turns(monkeypatch)

    run_async(monkeypatch, cfg, ["/logout", "/save"], live_settings=copy.deepcopy(store))

    assert turns == []
    reloaded = setting_layers.collect_settings(config_paths=[paths.global_config_file()], env={})

    assert all(settings.knob(reloaded, key) is None for key in ("PROVIDER", "BASE_URL", "API_KEY"))
