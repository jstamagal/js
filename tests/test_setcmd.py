from __future__ import annotations

import copy

import pytest

from js import setcmd, settings


@pytest.mark.parametrize(
    ("key", "raw", "expected"),
    [
        ("model.id", "local-model", "local-model"),
        ("limits.fetch_timeout_s", "20", 20),
        (
            "limits.shell_env_allow",
            '["PATH","HOME","FORGECODE_TOKEN"]',
            ["PATH", "HOME", "FORGECODE_TOKEN"],
        ),
        ("limits.inline_code_timeout_s", "300", 300),
        ("compact.notify_threshold", "0.75", 0.75),
        ("runtime.trace", "off", False),
        (
            "tools.alias_profiles",
            '[{"match":["openai"],"aliases":{"read":"r"}}]',
            [{"match": ["openai"], "aliases": {"read": "r"}}],
        ),
        ("provider.extra", '{"mode":"fast"}', {"mode": "fast"}),
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
    assert shown.lines[0] == f"{key} = {setcmd.render_value(spec, expected)}"
    assert shown.lines[1].strip() == spec.doc


@pytest.mark.parametrize("raw", ['"PATH"', '["PATH",""]', '["PATH",7]'])
def test_shell_env_allow_rejects_values_that_are_not_variable_name_lists(raw):
    live_settings = settings.seed_defaults()

    result = setcmd.set_command(live_settings, f"limits.shell_env_allow {raw}")

    assert result.error == (
        "limits.shell_env_allow: expected a JSON list of non-empty "
        "environment-variable names"
    )


def test_tools_alias_profiles_rejects_non_list_json():
    live_settings = settings.seed_defaults()
    before = copy.deepcopy(live_settings)
    raw = '{"match":["openai"],"aliases":{"read":"r"}}'

    result = setcmd.set_command(
        live_settings,
        f"tools.alias_profiles {raw}",
    )
    config_settings = settings.seed_defaults()
    config_before = copy.deepcopy(config_settings)
    config_result = setcmd.apply_config_line(
        config_settings,
        f"set tools.alias_profiles {raw}",
    )

    assert result.handled is True
    assert result.changed is False
    assert result.error == "tools.alias_profiles: expected a JSON list"
    assert live_settings == before
    assert config_result.handled is True
    assert config_result.changed is False
    assert config_result.error == "tools.alias_profiles: expected a JSON list"
    assert config_settings == config_before


def test_tools_alias_profiles_rejects_entries_without_aliases():
    live_settings = settings.seed_defaults()
    before = copy.deepcopy(live_settings)
    raw = '[{"match":["openai"]}]'

    result = setcmd.set_command(live_settings, f"tools.alias_profiles {raw}")
    config_settings = settings.seed_defaults()
    config_before = copy.deepcopy(config_settings)
    config_result = setcmd.apply_config_line(config_settings, f"set tools.alias_profiles {raw}")

    assert result.handled is True
    assert result.changed is False
    assert result.error == "tools.alias_profiles: expected profiles with match and aliases"
    assert live_settings == before
    assert config_result.handled is True
    assert config_result.changed is False
    assert config_result.error == "tools.alias_profiles: expected profiles with match and aliases"
    assert config_settings == config_before


def test_tools_alias_profiles_rejects_empty_alias_maps():
    live_settings = settings.seed_defaults()
    before = copy.deepcopy(live_settings)
    raw = '[{"match":["openai"],"aliases":{}}]'

    result = setcmd.set_command(live_settings, f"tools.alias_profiles {raw}")
    config_settings = settings.seed_defaults()
    config_before = copy.deepcopy(config_settings)
    config_result = setcmd.apply_config_line(config_settings, f"set tools.alias_profiles {raw}")

    assert result.handled is True
    assert result.changed is False
    assert result.error == "tools.alias_profiles: expected non-empty aliases"
    assert live_settings == before
    assert config_result.handled is True
    assert config_result.changed is False
    assert config_result.error == "tools.alias_profiles: expected non-empty aliases"
    assert config_settings == config_before


def test_tools_alias_profiles_rejects_empty_match_values():
    live_settings = settings.seed_defaults()
    before = copy.deepcopy(live_settings)
    raw = '[{"match":[],"aliases":{"read":"Read"}}]'

    result = setcmd.set_command(live_settings, f"tools.alias_profiles {raw}")
    config_settings = settings.seed_defaults()
    config_before = copy.deepcopy(config_settings)
    config_result = setcmd.apply_config_line(config_settings, f"set tools.alias_profiles {raw}")

    assert result.handled is True
    assert result.changed is False
    assert result.error == "tools.alias_profiles: expected non-empty match values"
    assert live_settings == before
    assert config_result.handled is True
    assert config_result.changed is False
    assert config_result.error == "tools.alias_profiles: expected non-empty match values"
    assert config_settings == config_before


def test_tools_alias_profiles_rejects_duplicate_alias_names():
    live_settings = settings.seed_defaults()
    before = copy.deepcopy(live_settings)
    raw = '[{"match":["openai"],"aliases":{"read":"Tool","write":"tool"}}]'

    result = setcmd.set_command(live_settings, f"tools.alias_profiles {raw}")
    config_settings = settings.seed_defaults()
    config_before = copy.deepcopy(config_settings)
    config_result = setcmd.apply_config_line(config_settings, f"set tools.alias_profiles {raw}")

    assert result.handled is True
    assert result.changed is False
    assert result.error == "tools.alias_profiles: expected unique alias names"
    assert live_settings == before
    assert config_result.handled is True
    assert config_result.changed is False
    assert config_result.error == "tools.alias_profiles: expected unique alias names"
    assert config_settings == config_before


def test_tools_alias_profiles_rejects_invalid_alias_names():
    live_settings = settings.seed_defaults()
    before = copy.deepcopy(live_settings)
    raw = '[{"match":["openai"],"aliases":{"read":"read file"}}]'

    result = setcmd.set_command(live_settings, f"tools.alias_profiles {raw}")
    config_settings = settings.seed_defaults()
    config_before = copy.deepcopy(config_settings)
    config_result = setcmd.apply_config_line(config_settings, f"set tools.alias_profiles {raw}")

    assert result.handled is True
    assert result.changed is False
    assert result.error == "tools.alias_profiles: expected alias names matching [A-Za-z0-9_-]+"
    assert live_settings == before
    assert config_result.handled is True
    assert config_result.changed is False
    assert config_result.error == "tools.alias_profiles: expected alias names matching [A-Za-z0-9_-]+"
    assert config_settings == config_before


def test_tools_alias_profiles_rejects_invalid_canonical_names():
    live_settings = settings.seed_defaults()
    before = copy.deepcopy(live_settings)
    raw = '[{"match":["openai"],"aliases":{"read file":"Read"}}]'

    result = setcmd.set_command(live_settings, f"tools.alias_profiles {raw}")
    config_settings = settings.seed_defaults()
    config_before = copy.deepcopy(config_settings)
    config_result = setcmd.apply_config_line(config_settings, f"set tools.alias_profiles {raw}")

    assert result.handled is True
    assert result.changed is False
    assert result.error == "tools.alias_profiles: expected canonical tool names matching [A-Za-z0-9_-]+"
    assert live_settings == before
    assert config_result.handled is True
    assert config_result.changed is False
    assert config_result.error == "tools.alias_profiles: expected canonical tool names matching [A-Za-z0-9_-]+"
    assert config_settings == config_before


def test_provider_extra_rejects_non_object_json():
    live_settings = settings.seed_defaults()
    before = copy.deepcopy(live_settings)
    raw = '["extra_body"]'

    result = setcmd.set_command(live_settings, f"provider.extra {raw}")
    config_settings = settings.seed_defaults()
    config_before = copy.deepcopy(config_settings)
    config_result = setcmd.apply_config_line(config_settings, f"set provider.extra {raw}")

    assert result.handled is True
    assert result.changed is False
    assert result.error == "provider.extra: expected a JSON object"
    assert live_settings == before
    assert config_result.handled is True
    assert config_result.changed is False
    assert config_result.error == "provider.extra: expected a JSON object"
    assert config_settings == config_before


def test_bool_off_is_valid_but_no_longer_a_magic_clear_token_for_other_types():
    # RULING A: magic strings die. "off" still parses as a real bool for a
    # bool knob, but for a nullable int/str knob it's just a bad value now —
    # `set -key` (js/setcmd.py apply_unset) is the only way to clear one.
    live_settings = settings.seed_defaults()

    bool_result = setcmd.set_command(live_settings, "runtime.trace off")
    int_result = setcmd.set_command(live_settings, "model.max_output_tokens 123")
    rejected = setcmd.set_command(live_settings, "model.max_output_tokens off")

    assert bool_result.error is None
    assert bool_result.lines == ["runtime.trace = off"]
    assert settings.get_dotted(live_settings, ("runtime", "trace")) is False
    assert int_result.error is None
    assert int_result.lines == ["model.max_output_tokens = 123"]
    assert rejected.error == "model.max_output_tokens: expected an integer"
    assert rejected.changed is False
    assert settings.get_dotted(live_settings, ("model", "max_output_tokens")) == 123  # unchanged

    cleared = setcmd.set_command(live_settings, "-model.max_output_tokens")
    assert cleared.error is None
    assert cleared.lines == ["model.max_output_tokens = <none>"]
    assert settings.get_dotted(live_settings, ("model", "max_output_tokens")) is None


def test_subagent_max_workers_rejects_values_below_one():
    live_settings = settings.seed_defaults()

    rejected = setcmd.set_command(live_settings, "limits.subagent_max_workers 0")
    accepted = setcmd.set_command(live_settings, "limits.subagent_max_workers 1")

    assert rejected.error == "limits.subagent_max_workers: expected an integer >= 1"
    assert rejected.changed is False
    assert accepted.error is None
    assert accepted.lines == ["limits.subagent_max_workers = 1"]


@pytest.mark.parametrize("token", ["default", "auto", "none", "unset"])
def test_magic_strings_store_verbatim_for_string_knobs(token):
    # RULING A headline: a literal string knob like model.id stores these
    # words as-is instead of silently clearing to the built-in default.
    live_settings = settings.seed_defaults()

    result = setcmd.set_command(live_settings, f"model.id {token}")

    assert result.error is None
    assert result.lines == [f"model.id = {token}"]
    assert settings.get_dotted(live_settings, ("model", "id")) == token

    cleared = setcmd.set_command(live_settings, "-model.id")
    assert cleared.error is None
    assert settings.get_dotted(live_settings, ("model", "id")) is None


def test_empty_state_rendering_distinguishes_off_none_and_unset():
    live_settings = settings.seed_defaults()

    off = setcmd.show_lines(live_settings, "runtime.debug")
    none = setcmd.show_lines(live_settings, "provider.id")
    unset_spec = settings.SettingSpec(
        "sampling.temperature",
        "float",
        None,
        "Provider-default sampling temperature.",
        empty=settings.EMPTY_UNSET,
    )

    assert off.lines[0] == "runtime.debug = off"
    assert none.lines[0] == "provider.id = <none>"
    assert setcmd.render_value(unset_spec, None) == "<unset>"
    sampling = setcmd.show_lines(live_settings, "sampling.temperature")
    assert sampling.lines[0] == "sampling.temperature = <unset>"
    template = "\n".join(settings._template_lines())
    assert "# Per-turn sampling overrides. Default display is <unset>;" in template
    # RULING A: "unset" is no longer a magic clear-token, so the template no
    # longer suggests typing it as a settable value — the commented example
    # line is just the bare key, blank.
    assert "#set sampling.temperature" in template
    assert "#set sampling.temperature unset" not in template


def test_secret_values_are_masked_when_shown():
    live_settings = settings.seed_defaults()

    changed = setcmd.set_command(live_settings, "provider.api_key sk-test")
    shown = setcmd.show_lines(live_settings, "provider.api_key")

    assert changed.error is None
    assert changed.lines == ["provider.api_key = <set>"]
    assert settings.get_dotted(live_settings, ("provider", "api_key")) == "sk-test"
    assert shown.lines[0] == "provider.api_key = <set>"


@pytest.mark.parametrize("key", ["model.id.foo", "provider.id.foo", "limits.fetch_timeout_s.foo"])
def test_registered_non_map_subkeys_return_error_without_mutating_settings(key: str):
    live_settings = settings.seed_defaults()
    before = copy.deepcopy(live_settings)

    result = setcmd.set_command(live_settings, f"{key} value")

    assert result.handled is True
    assert result.changed is False
    assert result.error == f"unknown knob: {key}"
    assert live_settings == before


def test_map_sub_key_updates_parent_map_and_shows_parent():
    live_settings = settings.seed_defaults()

    changed = setcmd.set_command(live_settings, "provider.extra.organization /p")
    shown = setcmd.show_lines(live_settings, "provider.extra")

    assert changed.error is None
    assert changed.lines == ["provider.extra.organization = /p"]
    assert settings.get_dotted(live_settings, ("provider", "extra", "organization")) == "/p"
    assert shown.error is None
    assert shown.lines[0] == "provider.extra = organization=/p"


@pytest.mark.parametrize("line", ["show model.id", "on input set compact.auto off", "run something"])
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
        ("/model local/qwen", ("model", "id"), "local/qwen"),
        ("provider deepseek", ("provider", "id"), "deepseek"),
        ("baseurl http://localhost:8080/v1", ("provider", "base_url"), "http://localhost:8080/v1"),
    ],
)
def test_apply_config_line_applies_setting_short_names(line: str, path: tuple[str, ...], value: str):
    store = settings.seed_defaults()

    result = setcmd.apply_config_line(store, line)

    assert result.error is None
    assert result.changed_keys == [".".join(path)]
    assert settings.get_dotted(store, path) == value


def test_set_accepts_a_setting_short_name_for_its_key():
    store = settings.seed_defaults()

    result = setcmd.set_command(store, "model other/model")

    assert result.changed_keys == ["model.id"]
    assert settings.get_dotted(store, ("model", "id")) == "other/model"


def test_apply_config_line_rejects_set_without_value():
    result = setcmd.apply_config_line(settings.seed_defaults(), "set model.id")

    assert result.handled is True
    assert result.changed is False
    assert result.error == "set needs a key and value: 'set model.id'"


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
    if spec.key == "model.reasoning_effort":
        # restricted domain (RULING B): an arbitrary string is rejected, so
        # the generic str-fallback below doesn't apply to this one knob.
        return "high", "high"
    if spec.key == "provider.id":
        # validated domain: must name a known provider or saved login.
        return "deepseek", "deepseek"
    if spec.key == "provider.base_url":
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
    settings.set_dotted(cfg, ("sampling", "temperature"), 1.0)
    result = setcmd.set_command(cfg, "-sampling.temperature")
    assert result.changed is True
    assert result.changed_keys == ["sampling.temperature"]
    assert settings.get_dotted(cfg, ("sampling", "temperature")) is None


def test_set_dash_key_on_already_unset_is_noop():
    result = setcmd.set_command({}, "-sampling.temperature")
    assert result.changed is False
    assert result.changed_keys == []
    assert "already unset" in result.lines[0]


def test_set_dash_key_clears_map_subkey():
    cfg = {}
    settings.set_dotted(cfg, ("provider", "extra", "organization"), "/tmp/x")
    result = setcmd.set_command(cfg, "-provider.extra.organization")
    assert result.changed is True
    assert settings.get_dotted(cfg, ("provider", "extra", "organization")) is None


def test_set_dash_unknown_knob_errors():
    result = setcmd.set_command({}, "-nope.nope")
    assert result.error == "unknown knob: nope.nope"


# --------------------------------------------------------------------------
# canonical JS_<DOTTED> env parity (env <-> jsrc)
# --------------------------------------------------------------------------

def test_canonical_env_name_maps_dotted_key():
    assert settings.canonical_env_name("sampling.top_p") == "JS_SAMPLING_TOP_P"
    assert settings.canonical_env_name("limits.max_read_lines") == "JS_LIMITS_MAX_READ_LINES"


def test_canonical_env_sets_knob_without_hand_picked_alias():
    # limits.max_read_lines has no short alias; the canonical name must still work.
    overlaid = settings.apply_env_overrides(
        settings.seed_defaults(), {"JS_LIMITS_MAX_READ_LINES": "42"}
    )
    assert settings.get_dotted(overlaid, ("limits", "max_read_lines")) == 42


def test_hand_picked_env_alias_wins_over_canonical():
    overlaid = settings.apply_env_overrides(
        settings.seed_defaults(),
        {"JS_MODEL": "from-alias", "JS_MODEL_ID": "from-canonical"},
    )
    assert settings.get_dotted(overlaid, ("model", "id")) == "from-alias"


def test_provider_id_and_base_url_validate_at_set_time():
    live_settings = settings.seed_defaults()

    bad_id = setcmd.set_command(live_settings, "provider.id not-a-provider-anywhere")
    assert bad_id.error is not None
    assert "unknown provider id" in bad_id.error
    assert settings.get_dotted(live_settings, ("provider", "id"), None) is None

    ok_id = setcmd.set_command(live_settings, "provider.id deepseek")
    assert ok_id.error is None

    bad_url = setcmd.set_command(live_settings, "provider.base_url http//localhost:8050/v1")
    assert bad_url.error is not None
    assert "http://" in bad_url.error

    ok_url = setcmd.set_command(live_settings, "provider.base_url http://localhost:8050/v1")
    assert ok_url.error is None


def test_editing_mode_accepts_emacs_or_vi_only():
    store = settings.seed_defaults()

    assert setcmd.set_command(store, "ui.editing_mode vi").error is None
    assert setcmd.set_command(store, "ui.editing_mode vim").error is not None
    assert settings.get_dotted(store, ("ui", "editing_mode")) == "vi"
