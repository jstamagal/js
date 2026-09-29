"""Effective `/set` view + `/save` round-trip.

The settings store lies: a `--model` flag, a saved-login base URL, and env-read
sampling (JS_TEMP ...) feed the next turn without ever landing in the store.
`/set` overlays those live sources; `/save` folds them back into a jsrc so a
reload reproduces the running configuration.
"""

from __future__ import annotations

import copy

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
    state = {"messages": [], "system": "sys", "settings": settings.seed_defaults(), "sampling_cli": Sampling()}
    for line in ("/set", "/show", "/set model.id", "/show provider.id"):
        before = copy.deepcopy(state["settings"])
        assert cli._handle_command(line, state, cfg) is True
        assert state["settings"] == before

    cli._handle_command("/set sampling.temperature 0.3", state, cfg)
    assert settings.get_dotted(state["settings"], ("sampling", "temperature")) == 0.3
    assert state["sampling_cli"].temperature == 0.3

    cli._handle_command("/set -sampling.temperature", state, cfg)
    assert settings.get_dotted(state["settings"], ("sampling", "temperature")) is None
    assert state["sampling_cli"].temperature is None


# ---------------------------------------------------------------------------
# Effective view rendering
# ---------------------------------------------------------------------------

def test_show_lines_effective_annotates_and_masks():
    store = settings.seed_defaults()
    settings.set_dotted(store, ("model", "id"), "stored/model")
    settings.set_dotted(store, ("provider", "api_key"), "private-store-key")
    settings.set_dotted(store, ("provider", "id"), "stored-provider")
    overlay = {"model.id": setcmd.LiveValue("live/model", "--model flag")}

    one = setcmd.show_lines_effective(store, overlay, "model.id")
    rendered = "\n".join(one.lines)
    assert one.error is None
    assert "live/model" in rendered
    assert "--model flag" in rendered
    assert "stored/model" not in rendered

    full = setcmd.show_lines_effective(store, overlay)
    rendered = "\n".join(full.lines)
    assert "live/model" in rendered
    assert "--model flag" in rendered
    assert "provider.api_key" in rendered
    assert "private-store-key" not in rendered
    assert "stored/model" not in rendered

    overlay["provider.api_key"] = setcmd.LiveValue("masked-login-fixture", "saved-login-fixture")
    secret = setcmd.show_lines_effective(store, overlay, "provider.api_key")
    displayed = "\n".join(secret.lines)
    assert "masked-login-fixture" in displayed
    assert "saved-login-fixture" in displayed
    assert "private-store-key" not in displayed

    # Values without an overlay still reflect the explicitly supplied store.
    plain = setcmd.show_lines_effective(store, overlay, "provider.id")
    assert "stored-provider" in "\n".join(plain.lines)


def test_show_lines_effective_unknown_key_errors():
    result = setcmd.show_lines_effective(settings.seed_defaults(), {}, "nope.nope")
    assert result.error == msgs.UNKNOWN_SETTING.text(key="nope.nope")


# ---------------------------------------------------------------------------
# Overlay computation (the live route diffed against the store)
# ---------------------------------------------------------------------------

def test_overlay_env_sampling_shows_live_value():
    store = settings.seed_defaults()  # sampling.temperature unset
    overlay = cli._live_settings_overlay(
        store,
        live_model=None,
        model_source=None,
        provider_id=None,
        provider_base_url=None,
        provider_api_key=None,
        saved_login=None,
        provider_default_base_url=None,
        eff_sampling=Sampling(temperature=1.0),
        env_sampling=Sampling(temperature=1.0),
        manifest_sampling=Sampling(),
        environ={"JS_TEMP": "1.0"},
    )
    live = overlay["sampling.temperature"]
    assert live.display == "1.0"
    assert live.source == "env JS_TEMP"


def test_overlay_flag_model_shows_source_tag():
    store = settings.seed_defaults()
    settings.set_dotted(store, ("model", "id"), "stored/model")
    overlay = cli._live_settings_overlay(
        store,
        live_model="testes/test",
        model_source="--model flag",
        provider_id=None,
        provider_base_url=None,
        provider_api_key=None,
        saved_login=None,
        provider_default_base_url=None,
        eff_sampling=Sampling(),
        env_sampling=Sampling(),
        manifest_sampling=Sampling(),
        environ={},
    )
    assert overlay["model.id"] == setcmd.LiveValue("testes/test", "--model flag")


def test_overlay_login_provider_and_masked_key():
    class _Login:
        provider_base_url = "http://localhost:8050/v1"
        provider_api_key = "sk-login"

    store = settings.seed_defaults()
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
    assert overlay["provider.id"].source == "login testes"
    assert overlay["provider.base_url"] == setcmd.LiveValue(
        "http://localhost:8050/v1", "login testes"
    )
    # secret is never printed in the clear
    assert overlay["provider.api_key"].display
    assert "sk-login" not in overlay["provider.api_key"].display
    assert overlay["provider.api_key"].source == "login testes"


def test_overlay_empty_when_store_matches_effective():
    store = settings.seed_defaults()
    settings.set_dotted(store, ("model", "id"), "same/model")
    settings.set_dotted(store, ("sampling", "temperature"), 0.7)
    overlay = cli._live_settings_overlay(
        store,
        live_model="same/model",
        model_source=None,
        provider_id=None,
        provider_base_url=None,
        provider_api_key=None,
        saved_login=None,
        provider_default_base_url=None,
        eff_sampling=Sampling(temperature=0.7),
        env_sampling=Sampling(),
        manifest_sampling=Sampling(),
        environ={},
    )
    assert overlay == {}


# ---------------------------------------------------------------------------
# /save writer
# ---------------------------------------------------------------------------

def _effective_store() -> dict:
    store = settings.seed_defaults()
    settings.set_dotted(store, ("model", "id"), "testes/test")
    settings.set_dotted(store, ("provider", "id"), "deepseek")
    settings.set_dotted(store, ("provider", "base_url"), "http://localhost:8050/v1")
    settings.set_dotted(store, ("provider", "api_key"), "sk-secret")
    settings.set_dotted(store, ("sampling", "temperature"), 1.0)
    return store


def test_save_round_trip_reproduces_effective_values(tmp_path):
    store = _effective_store()
    path = tmp_path / "jsrc"

    count, backup = settings.save_settings_to_jsrc(path, store, stamp="2026-07-07 12:00")

    assert backup is None  # no prior file to back up
    assert count == 5

    reloaded = settings.collect_settings(config_paths=[path], env={})
    for key in (
        "model.id",
        "provider.id",
        "provider.base_url",
        "provider.api_key",
        "sampling.temperature",
    ):
        spec = settings.SPEC_BY_KEY[key]
        assert settings.get_dotted(reloaded, spec.path) == settings.get_dotted(store, spec.path)


def test_save_backs_up_existing_file(tmp_path):
    path = tmp_path / "jsrc"
    path.write_text("# prior config\nset model.id old\n", encoding="utf-8")

    _count, backup = settings.save_settings_to_jsrc(path, _effective_store(), stamp="x")

    assert backup is not None
    assert backup.name == "jsrc.bak"
    assert backup.read_text(encoding="utf-8") == "# prior config\nset model.id old\n"


def test_save_omits_defaults_but_writes_secret(tmp_path):
    path = tmp_path / "jsrc"
    settings.save_settings_to_jsrc(path, _effective_store(), stamp="x")
    text = path.read_text(encoding="utf-8")

    # a knob left at its built-in default is never written
    assert "runtime.trace" not in text
    assert "limits.max_tool_iterations" not in text
    # provider.api_key IS persisted verbatim (his box; the jsrc key line is plain)
    assert "set provider.api_key sk-secret" in text
