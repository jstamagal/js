from __future__ import annotations

import copy

import pytest

from js import setcmd, settings
from js import messages as msgs


def _is(key: str, value: str) -> str:
    return msgs.SETTING_IS.line(colour=False, key=key, value=value)


def _changed(key: str, value: str) -> str:
    return msgs.SETTING_CHANGED.line(colour=False, key=key, value=value)


@pytest.mark.parametrize(
    ("key", "raw", "expected"),
    [
        ("MODEL", "local-model", "local-model"),
        ("FETCH_TIMEOUT", "20", 20),
        (
            "SHELL_ENV_KEEP",
            '["PATH","HOME","FORGECODE_TOKEN"]',
            ["PATH", "HOME", "FORGECODE_TOKEN"],
        ),
        ("INLINE_CODE_TIMEOUT", "300", 300),
        ("COMPACTION_WARN_AT", "0.75", 0.75),
        ("TRANSCRIPT_LOG", "off", False),
        (
            "TOOL_ALIASES",
            '[{"match":["openai"],"aliases":{"read":"r"}}]',
            [{"match": ["openai"], "aliases": {"read": "r"}}],
        ),
        ("EXTRA_PARAMS", '{"mode":"fast"}', {"mode": "fast"}),
    ],
)
def test_set_and_show_roundtrip_per_registry_type(key: str, raw: str, expected):
    live_settings = settings.seed_defaults()
    spec = settings.SPEC_BY_KEY[key]

    changed = setcmd.set_command(live_settings, f"{key} {raw}")
    shown = setcmd.show_lines(live_settings, f"{key}")

    assert changed.handled is True
    assert changed.changed is True
    assert changed.error is None
    assert settings.get_dotted(live_settings, spec.path) == expected
    assert shown.error is None
    assert shown.lines == [_is(key.upper(), setcmd.render_value(spec, expected, masked=False))]


@pytest.mark.parametrize("raw", ['"PATH"', '["PATH",""]', '["PATH",7]'])
def test_shell_env_allow_rejects_values_that_are_not_variable_name_lists(raw):
    live_settings = settings.seed_defaults()

    result = setcmd.set_command(live_settings, f"SHELL_ENV_KEEP {raw}")

    assert result.error == (
        "SHELL_ENV_KEEP: expected a JSON list of non-empty "
        "environment-variable names"
    )


def test_tools_alias_profiles_rejects_non_list_json():
    live_settings = settings.seed_defaults()
    before = copy.deepcopy(live_settings)
    raw = '{"match":["openai"],"aliases":{"read":"r"}}'

    result = setcmd.set_command(
        live_settings,
        f"TOOL_ALIASES {raw}",
    )
    config_settings = settings.seed_defaults()
    config_before = copy.deepcopy(config_settings)
    config_result = setcmd.apply_config_line(
        config_settings,
        f"set TOOL_ALIASES {raw}",
    )

    assert result.handled is True
    assert result.changed is False
    assert result.error == "TOOL_ALIASES: expected a JSON list"
    assert live_settings == before
    assert config_result.handled is True
    assert config_result.changed is False
    assert config_result.error == "TOOL_ALIASES: expected a JSON list"
    assert config_settings == config_before


def test_provider_extra_rejects_non_object_json():
    live_settings = settings.seed_defaults()
    before = copy.deepcopy(live_settings)
    raw = '["extra_body"]'

    result = setcmd.set_command(live_settings, f"EXTRA_PARAMS {raw}")
    config_settings = settings.seed_defaults()
    config_before = copy.deepcopy(config_settings)
    config_result = setcmd.apply_config_line(config_settings, f"set EXTRA_PARAMS {raw}")

    assert result.handled is True
    assert result.changed is False
    assert result.error == "EXTRA_PARAMS: expected a JSON object"
    assert live_settings == before
    assert config_result.handled is True
    assert config_result.changed is False
    assert config_result.error == "EXTRA_PARAMS: expected a JSON object"
    assert config_settings == config_before


def test_subagent_max_workers_rejects_values_below_one():
    live_settings = settings.seed_defaults()

    rejected = setcmd.set_command(live_settings, "SUBAGENT_WORKERS_MAX 0")
    accepted = setcmd.set_command(live_settings, "SUBAGENT_WORKERS_MAX 1")

    assert rejected.error == "SUBAGENT_WORKERS_MAX: expected an integer >= 1"
    assert rejected.changed is False
    assert accepted.error is None
    assert accepted.lines == [_changed("SUBAGENT_WORKERS_MAX", "1")]


def test_secret_values_are_masked_when_shown():
    live_settings = settings.seed_defaults()
    setcmd.set_command(live_settings, "PARANOID_MODE on")

    changed = setcmd.set_command(live_settings, "API_KEY sk-test")
    shown = setcmd.show_lines(live_settings, "API_KEY")

    assert changed.error is None
    assert changed.lines == [_changed("API_KEY", "<set>")]
    assert settings.get_dotted(live_settings, ("API_KEY",)) == "sk-test"
    assert shown.lines[0] == _is("API_KEY", "<set>")


@pytest.mark.parametrize("key", ["MODEL.foo", "PROVIDER.foo", "FETCH_TIMEOUT.foo"])
def test_registered_non_map_subkeys_return_error_without_mutating_settings(key: str):
    live_settings = settings.seed_defaults()
    before = copy.deepcopy(live_settings)

    result = setcmd.set_command(live_settings, f"{key} value")

    assert result.handled is True
    assert result.changed is False
    assert result.error == msgs.UNKNOWN_SETTING.text(key=key)
    assert live_settings == before


def test_map_sub_key_updates_parent_map_and_shows_parent():
    live_settings = settings.seed_defaults()

    changed = setcmd.set_command(live_settings, "EXTRA_PARAMS.organization /p")
    shown = setcmd.show_lines(live_settings, "EXTRA_PARAMS")

    assert changed.error is None
    assert changed.lines == [_changed("EXTRA_PARAMS.organization", "/p")]
    assert settings.get_dotted(live_settings, ("EXTRA_PARAMS", "organization")) == "/p"
    assert shown.error is None
    assert shown.lines[0] == _is("EXTRA_PARAMS", "organization=/p")


@pytest.mark.parametrize("line", ["show MODEL", "on input set COMPACTION_AUTO off", "run something"])
def test_apply_config_line_leaves_other_commands_to_the_command_table(line: str):
    before = settings.seed_defaults()
    store = settings.seed_defaults()

    result = setcmd.apply_config_line(store, line)

    assert result.handled is False
    assert result.error is None
    assert store == before


@pytest.mark.parametrize(
    ("line", "path", "value"),
    [
        ("/model local/qwen", ("MODEL",), "local/qwen"),
        ("provider deepseek", ("PROVIDER",), "deepseek"),
        ("baseurl http://localhost:8080/v1", ("BASE_URL",), "http://localhost:8080/v1"),
    ],
)
def test_apply_config_line_applies_setting_short_names(line: str, path: tuple[str, ...], value: str):
    store = settings.seed_defaults()

    result = setcmd.apply_config_line(store, line)

    assert result.error is None
    assert result.changed_keys == [".".join(path)]
    assert settings.get_dotted(store, path) == value


def test_apply_config_line_rejects_set_without_value():
    result = setcmd.apply_config_line(settings.seed_defaults(), "set MODEL")

    assert result.handled is True
    assert result.changed is False
    assert result.error == msgs.SET_NEEDS_TWO.text(line="set MODEL")


def test_registry_defaults_seed_and_env_overrides_roundtrip():
    seeded = settings.seed_defaults()
    missing = object()

    for spec in settings.REGISTRY:
        value = settings.get_dotted(seeded, spec.path, missing)
        if spec.default is None:
            assert value is missing
            continue
        assert value == spec.default
        if isinstance(spec.default, (dict, list)):
            assert value is not spec.default

    for spec in settings.REGISTRY:
        if not spec.env:
            continue
        raw, expected = _env_case(spec)
        overlaid = settings.apply_env_overrides(settings.seed_defaults(), {spec.env: raw})
        assert settings.get_dotted(overlaid, spec.path) == expected


def _env_case(spec: settings.SettingSpec) -> tuple[str, object]:
    if spec.key == "EFFORT":
        # restricted domain (RULING B): an arbitrary string is rejected, so
        # the generic str-fallback below doesn't apply to this one knob.
        return "high", "high"
    if spec.key == "PROVIDER":
        # validated domain: must name a known provider or saved login.
        return "deepseek", "deepseek"
    if spec.key == "BASE_URL":
        # validated domain: must carry an http(s) scheme.
        return "http://env.test/v1", "http://env.test/v1"
    if spec.type == "bool":
        return "on", True
    if spec.type == "int":
        return "123", 123
    if spec.type == "float":
        return "0.25", 0.25
    if spec.type in {"json", "map"}:
        return '{"env": true}', {"env": True}
    return f"env-{spec.key}", f"env-{spec.key}"


# --------------------------------------------------------------------------
# `set -key` unset
# --------------------------------------------------------------------------

def test_set_dash_key_unsets_registered_knob():
    cfg = {}
    settings.set_dotted(cfg, ("TEMPERATURE",), 1.0)
    result = setcmd.set_command(cfg, "-TEMPERATURE")
    assert result.changed is True
    assert result.changed_keys == ["TEMPERATURE"]
    assert settings.get_dotted(cfg, ("TEMPERATURE",)) is None


def test_set_dash_key_on_already_unset_is_noop():
    result = setcmd.set_command({}, "-TEMPERATURE")
    assert result.changed is False
    assert result.changed_keys == []


def test_set_dash_key_clears_map_subkey():
    cfg = {}
    settings.set_dotted(cfg, ("EXTRA_PARAMS", "organization"), "/tmp/x")
    result = setcmd.set_command(cfg, "-EXTRA_PARAMS.organization")
    assert result.changed is True
    assert settings.get_dotted(cfg, ("EXTRA_PARAMS", "organization")) is None


def test_set_dash_unknown_knob_errors():
    result = setcmd.set_command({}, "-nope.nope")
    assert result.error == msgs.UNKNOWN_SETTING.text(key="nope.nope")


# --------------------------------------------------------------------------
# env: setting NAME is env JS_NAME
# --------------------------------------------------------------------------

def test_env_js_name_sets_the_setting():
    overlaid = settings.apply_env_overrides(settings.seed_defaults(), {"JS_READ_LINES_MAX": "42"})
    assert overlaid["READ_LINES_MAX"] == 42


def test_provider_id_and_base_url_validate_at_set_time():
    live_settings = settings.seed_defaults()

    bad_id = setcmd.set_command(live_settings, "PROVIDER not-a-provider-anywhere")
    assert bad_id.error is not None
    assert settings.get_dotted(live_settings, ("PROVIDER",), None) is None

    ok_id = setcmd.set_command(live_settings, "PROVIDER deepseek")
    assert ok_id.error is None

    bad_url = setcmd.set_command(live_settings, "BASE_URL http//localhost:8050/v1")
    assert bad_url.error is not None
    assert "http://" in bad_url.error

    ok_url = setcmd.set_command(live_settings, "BASE_URL http://localhost:8050/v1")
    assert ok_url.error is None


def test_editing_mode_accepts_emacs_or_vi_only():
    store = settings.seed_defaults()

    assert setcmd.set_command(store, "EDITING_MODE vi").error is None
    assert setcmd.set_command(store, "EDITING_MODE vim").error is not None
    assert settings.get_dotted(store, ("EDITING_MODE",)) == "vi"


def test_a_config_refuses_a_settings_key_that_is_not_a_setting(tmp_path):
    from dataclasses import replace

    from test_lazy_tool_discovery import _cfg

    cfg = _cfg(tmp_path)
    assert replace(cfg, settings={"RETRY_COUNT_MAX": 3}).settings == {"RETRY_COUNT_MAX": 3}
    with pytest.raises(ValueError, match="retry_attempts"):
        replace(cfg, settings={"retry_attempts": 3})


def test_a_config_takes_a_display_setting_and_map_contents(tmp_path):
    from dataclasses import replace

    from test_lazy_tool_discovery import _cfg

    store = {"TRACE": True, "EXTRA_PARAMS": {"a": {"b": 1}}, "CONTEXT_SIZE_PER_MODEL": {"x/y": 9}}
    assert replace(_cfg(tmp_path), settings=store).settings == store
