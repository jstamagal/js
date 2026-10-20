"""PARANOID_MODE where js reads it outside a turn: js --login and install."""

from __future__ import annotations

import json

import pytest

from pathlib import Path
from types import SimpleNamespace

from js import login_cli, logins, providers


def test_login_test_request_carries_extra_headers_and_the_scrub_header(monkeypatch):
    monkeypatch.setenv("JS_EXTRA_HEADERS", "X-Pool: fofa")
    monkeypatch.setenv("JS_PARANOID_MODE", "extra")
    sent = {}

    def fake_stream_model(**kwargs):
        sent.update(kwargs)
        return SimpleNamespace(text="2")

    monkeypatch.setattr(login_cli.model_client, "stream_model", fake_stream_model)
    login = logins.Login(provider_id="vllm", provider_base_url="http://x/v1", provider_headers={"X-Saved": "1"})
    assert login_cli._run_secondary_test(login, providers.provider_for_login("vllm"), "m")
    assert sent["provider_headers"] == {"X-Saved": "1", "X-Pool": "fofa", "X-JS-Scrub": "on"}


def test_project_jsrc_paranoid_mode_masks_login_keys(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("JS_PARANOID_MODE", raising=False)
    project = tmp_path / "project"
    (project / ".js").mkdir(parents=True)
    (project / ".js" / "jsrc").write_text("set PARANOID_MODE on\n")
    monkeypatch.chdir(project)
    assert login_cli._mask("sk-1234567890abcd") != "sk-1234567890abcd"


SENTINEL = "SENTINEL-SECRET-4417"
SECRET_SAMPLES = {
    "API_KEY": SENTINEL,
    "EXTRA_HEADERS": f"Authorization: Bearer {SENTINEL}",
    "EXTRA_PARAMS": json.dumps({"api_key": SENTINEL}),
    "MCP_SERVERS": json.dumps({"x": {"command": "srv", "env": {"TOKEN": SENTINEL}}}),
    "ENDPOINT_DEFAULT_KEY": SENTINEL,
    "SERPER_API_KEY": SENTINEL,
    "TAVILY_API_KEY": SENTINEL,
    "EXA_API_KEY": SENTINEL,
    "CONTEXT7_API_KEY": SENTINEL,
    "TYPESAFE_API_KEY": SENTINEL,
}


def test_every_secret_setting_is_masked_everywhere_settings_are_shown(tmp_path):
    from dataclasses import replace

    from js import compaction_flight, settings, setcmd
    from test_config_compaction_layers import _compact_test_cfg

    secret_keys = {spec.key for spec in settings.REGISTRY if spec.secret}
    assert set(SECRET_SAMPLES) == secret_keys  # a new secret setting needs a sample here

    for key, raw in SECRET_SAMPLES.items():
        store = settings.seed_defaults()
        setcmd.set_command(store, "PARANOID_MODE on")
        changed = setcmd.set_command(store, f"{key} {raw}")
        assert changed.error is None, (key, changed.error)
        printed = [*changed.lines, *setcmd.show_lines(store, key).lines, *setcmd.show_lines(store).lines,
                   *setcmd.show_lines_effective(store).lines]
        assert not any(SENTINEL in line for line in printed), key

        cfg = replace(_compact_test_cfg(tmp_path / key, {"COMPACTION_LOG_DIR": str(tmp_path / key / "f")}),
                      paranoid_mode="on")
        cfg.settings.update(store)
        flight = compaction_flight.CompactionFlight(
            cfg, "sys", [], trigger="test", forced=True, focus="", preserve_from=0, details={},
            directory=compaction_flight.flight_log_dir(cfg))
        flight.close()
        assert SENTINEL not in flight.path.read_text(), key


def test_an_extra_on_the_login_command_line_masks_the_login_screens(monkeypatch, tmp_path: Path):
    from js import cli, config

    monkeypatch.delenv("JS_PARANOID_MODE", raising=False)
    monkeypatch.chdir(tmp_path)
    seen = {}
    monkeypatch.setattr(login_cli, "main", lambda argv: seen.update(masked=login_cli._mask("sk-1234567890abcd")) or 0)
    try:
        assert cli.main(["--extra", "PARANOID_MODE=on", "--login=openai"]) == 0
    finally:
        config.set_invocation()
    assert seen["masked"] != "sk-1234567890abcd"


BAD_SECRET_VALUES = {
    "API_KEY": None,  # any string is a valid key
    "EXTRA_HEADERS": f"Authorization {SENTINEL}",
    "EXTRA_HEADERS escaped": json.dumps(f"X-Ok: 1\n{SENTINEL}"),
    "EXTRA_PARAMS": f"[{SENTINEL}",
    "MCP_SERVERS": f"{{{SENTINEL}",
    "ENDPOINT_DEFAULT_KEY": None,  # any string is a valid key
    "SERPER_API_KEY": None,
    "TAVILY_API_KEY": None,
    "EXA_API_KEY": None,
    "CONTEXT7_API_KEY": None,
    "TYPESAFE_API_KEY": None,
}


def test_an_invalid_value_for_a_secret_setting_is_never_echoed(monkeypatch):
    from js import settings, setcmd

    secret_keys = {spec.key for spec in settings.REGISTRY if spec.secret}
    assert {name.split()[0] for name in BAD_SECRET_VALUES} == secret_keys  # a new secret setting needs one here
    for name, bad in BAD_SECRET_VALUES.items():
        key = name.split()[0]
        if bad is None:
            continue
        for mode in ("off", "on", "extra"):
            store = settings.seed_defaults()
            setcmd.set_command(store, f"PARANOID_MODE {mode}")
            result = setcmd.set_command(store, f"{key} {bad}")
            assert result.error and SENTINEL not in result.error, (key, mode, result.error)
        with pytest.raises(ValueError) as caught:
            settings.parse_extra_arg(f"{key}={bad}")
        assert SENTINEL not in str(caught.value), key
        warned: list[str] = []
        with monkeypatch.context() as patch:
            patch.setattr(settings.msgs, "warn", lambda *a, **k: warned.append(repr((a, k))))
            settings.apply_env_overrides({}, {f"JS_{key}": bad})
        assert warned and not any(SENTINEL in w for w in warned), key
