from __future__ import annotations

import threading
from types import SimpleNamespace


from js import settings, turn_settings
from js.turn_settings import INVALID, TURN_SETTINGS, inherit, install, project


def _store(**values) -> dict:
    store: dict = {}
    for key, value in values.items():
        settings.set_dotted(store, tuple(key.split(".")), value)
    return store


def test_every_row_names_a_registered_setting():
    for row in TURN_SETTINGS:
        assert settings.spec_for(row.key) is not None, row.key


def test_every_limits_setting_has_a_row():
    rows = {row.key for row in TURN_SETTINGS}
    limits = {spec.key for spec in settings.REGISTRY if spec.section == "limits"}

    assert limits - rows == set()


def test_rows_have_distinct_attributes():
    attrs = [row.attr for row in TURN_SETTINGS]

    assert len(attrs) == len(set(attrs))


def test_every_js_jsrc_value_reads_as_valid():
    for row in TURN_SETTINGS:
        assert row.default() is not INVALID, row.key


def test_projecting_the_defaults_gives_each_rows_default():
    projected = project(settings.seed_defaults())

    assert projected == {row.attr: row.default() for row in TURN_SETTINGS}


def test_an_empty_store_projects_the_defaults():
    assert project(None) == project(settings.seed_defaults())


def test_a_readable_integer_is_used():
    assert project(_store(**{"FETCH_TIMEOUT": "7"}))["fetch_timeout_s"] == 7


def test_a_missing_or_unreadable_value_falls_back_to_the_fallback_attribute():
    fallback = SimpleNamespace(**{row.attr: f"fallback-{row.attr}" for row in TURN_SETTINGS})

    projected = project(_store(**{"READ_LINES_MAX": "abc", "FETCH_TIMEOUT": 9}), fallback)

    assert projected["max_read_lines"] == "fallback-max_read_lines"
    assert projected["fetch_timeout_s"] == 9
    assert projected["user_agent"] == "fallback-user_agent"


def test_max_parallel_tools_is_at_least_one():
    assert project(_store(**{"PARALLEL_TOOLS_MAX": 0}))["max_parallel_tools"] == 1


def test_an_unset_optional_integer_stays_unset():
    fallback = SimpleNamespace(**{row.attr: 1 for row in TURN_SETTINGS})

    assert project(_store(**{"OUTPUT_TOKENS_MAX": None}), fallback)["max_output_tokens"] is None


def test_install_copies_only_context_rows_the_cfg_has():
    context = SimpleNamespace(fetch_timeout_s=1, user_agent="kept")
    cfg = SimpleNamespace(fetch_timeout_s=42, max_tool_iterations=3)

    install(context, cfg)

    assert context.fetch_timeout_s == 42
    assert context.user_agent == "kept"
    assert not hasattr(context, "max_tool_iterations")


def test_inherit_returns_the_context_rows_of_the_parent():
    from js.toolkit import ToolContext

    parent = ToolContext(fetch_timeout_s=11, jail_bind=("/opt/x",))

    inherited = inherit(parent)

    assert inherited["fetch_timeout_s"] == 11
    assert inherited["jail_bind"] == ("/opt/x",)
    assert set(inherited) == {row.attr for row in TURN_SETTINGS if row.on_context}


def test_config_and_context_declare_the_rows_as_fields():
    import dataclasses

    from js.config import Config
    from js.toolkit import ToolContext

    config_fields = {f.name for f in dataclasses.fields(Config)}
    context_fields = {f.name for f in dataclasses.fields(ToolContext)}

    assert {row.attr for row in TURN_SETTINGS} <= config_fields
    assert {row.attr for row in TURN_SETTINGS if row.on_context} <= context_fields
    assert issubclass(Config, turn_settings.ConfigSettings)
    assert issubclass(ToolContext, turn_settings.ContextSettings)


# --- the store's values get set's range rules (ALL-CLAIMS.md:639-643) ---------


def test_an_empty_jail_bind_entry_is_refused():
    assert turn_settings.paths([""]) is INVALID
    assert turn_settings.paths(["  "]) is INVALID
    assert turn_settings.paths(["/a", "/b:ro"]) == ("/a", "/b:ro")


def test_live_settings_reads_config_files_at_most_once(monkeypatch):
    from js import config

    calls = []
    monkeypatch.setattr(config, "run_settings", lambda **kw: calls.append(kw) or {})
    monkeypatch.setattr(settings, "_policy_cache", None)
    settings.bind_live_settings(None)
    for _ in range(20):
        settings.live_value("SWARM_SPAWN_MAX")
    assert len(calls) == 1

    settings.bind_live_settings({"SWARM_SPAWN_MAX": 3})
    try:
        seen = []
        worker = threading.Thread(target=lambda: seen.append(settings.live_value("SWARM_SPAWN_MAX")))
        worker.start()
        worker.join()
        assert seen == [3] and len(calls) == 1
    finally:
        settings.bind_live_settings(None)
