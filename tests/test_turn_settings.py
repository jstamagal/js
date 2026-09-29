from __future__ import annotations

from types import SimpleNamespace

import pytest

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


@pytest.mark.parametrize("bad", [True, "abc", None, [3]])
def test_an_unreadable_integer_falls_back_to_the_default(bad):
    projected = project(_store(**{"limits.fetch_timeout_s": bad}))

    assert projected["fetch_timeout_s"] == settings.default_value("limits.fetch_timeout_s")


def test_a_readable_integer_is_used():
    assert project(_store(**{"limits.fetch_timeout_s": "7"}))["fetch_timeout_s"] == 7


def test_a_missing_or_unreadable_value_falls_back_to_the_fallback_attribute():
    fallback = SimpleNamespace(**{row.attr: f"fallback-{row.attr}" for row in TURN_SETTINGS})

    projected = project(_store(**{"limits.max_read_lines": "abc", "limits.fetch_timeout_s": 9}), fallback)

    assert projected["max_read_lines"] == "fallback-max_read_lines"
    assert projected["fetch_timeout_s"] == 9
    assert projected["user_agent"] == "fallback-user_agent"


def test_max_parallel_tools_is_at_least_one():
    assert project(_store(**{"runtime.max_parallel_tools": 0}))["max_parallel_tools"] == 1


def test_an_unknown_kernel_verbosity_falls_back():
    default = settings.default_value("kernel.verbosity")

    assert project(_store(**{"kernel.verbosity": "LOUD"}))["kernel_verbosity"] == default
    assert project(_store(**{"kernel.verbosity": " Verbose "}))["kernel_verbosity"] == "verbose"


def test_a_shell_env_allow_with_an_empty_name_falls_back():
    default = tuple(settings.default_value("limits.shell_env_allow"))

    assert project(_store(**{"limits.shell_env_allow": ["PATH", ""]}))["shell_env_allow"] == default
    assert project(_store(**{"limits.shell_env_allow": ["PATH"]}))["shell_env_allow"] == ("PATH",)


def test_an_unset_optional_integer_stays_unset():
    fallback = SimpleNamespace(**{row.attr: 1 for row in TURN_SETTINGS})

    assert project(_store(**{"model.max_output_tokens": None}), fallback)["max_output_tokens"] is None


def test_a_non_boolean_flag_falls_back():
    assert project(_store(**{"runtime.trace": "yes"}))["trace"] == settings.default_value("runtime.trace")


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
