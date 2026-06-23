from __future__ import annotations

import copy

import pytest

from js import setcmd, settings


@pytest.mark.parametrize(
    ("key", "raw", "expected"),
    [
        ("model.id", "local-model", "local-model"),
        ("limits.fetch_timeout_s", "20", 20),
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

    changed = setcmd.run_repl_command(live_settings, f"/set {key} {raw}")
    shown = setcmd.run_repl_command(live_settings, f"/show {key}")

    assert changed.handled is True
    assert changed.changed is True
    assert changed.error is None
    assert settings.get_dotted(live_settings, spec.path) == expected
    assert shown.error is None
    assert shown.lines == [f"{key} = {setcmd.render_value(spec, expected)}"]


def test_tools_alias_profiles_rejects_non_list_json():
    live_settings = settings.seed_defaults()
    before = copy.deepcopy(live_settings)
    raw = '{"match":["openai"],"aliases":{"read":"r"}}'

    result = setcmd.run_repl_command(
        live_settings,
        f"/set tools.alias_profiles {raw}",
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

    result = setcmd.run_repl_command(live_settings, f"/set tools.alias_profiles {raw}")
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

    result = setcmd.run_repl_command(live_settings, f"/set tools.alias_profiles {raw}")
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

    result = setcmd.run_repl_command(live_settings, f"/set tools.alias_profiles {raw}")
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

    result = setcmd.run_repl_command(live_settings, f"/set tools.alias_profiles {raw}")
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

    result = setcmd.run_repl_command(live_settings, f"/set tools.alias_profiles {raw}")
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

    result = setcmd.run_repl_command(live_settings, f"/set tools.alias_profiles {raw}")
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

    result = setcmd.run_repl_command(live_settings, f"/set provider.extra {raw}")
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


def test_bool_off_differs_from_nullable_off():
    live_settings = settings.seed_defaults()

    bool_result = setcmd.run_repl_command(live_settings, "/set runtime.trace off")
    int_result = setcmd.run_repl_command(live_settings, "/set model.max_output_tokens 123")
    none_result = setcmd.run_repl_command(live_settings, "/set model.max_output_tokens off")

    assert bool_result.error is None
    assert bool_result.lines == ["runtime.trace = off"]
    assert settings.get_dotted(live_settings, ("runtime", "trace")) is False
    assert int_result.error is None
    assert int_result.lines == ["model.max_output_tokens = 123"]
    assert none_result.error is None
    assert none_result.lines == ["model.max_output_tokens = <none>"]
    assert settings.get_dotted(live_settings, ("model", "max_output_tokens")) is None


def test_empty_state_rendering_distinguishes_off_none_and_unset():
    live_settings = settings.seed_defaults()

    off = setcmd.run_repl_command(live_settings, "/show runtime.debug")
    none = setcmd.run_repl_command(live_settings, "/show provider.id")
    unset_spec = settings.SettingSpec(
        "sampling.temperature",
        "float",
        None,
        "Provider-default sampling temperature.",
        empty=settings.EMPTY_UNSET,
    )

    assert off.lines == ["runtime.debug = off"]
    assert none.lines == ["provider.id = <none>"]
    assert setcmd.render_value(unset_spec, None) == "<unset>"
    sampling = setcmd.run_repl_command(live_settings, "/show sampling.temperature")
    assert sampling.lines == ["sampling.temperature = <unset>"]
    template = "\n".join(settings._template_lines())
    assert "# Per-turn sampling overrides. Default display is <unset>;" in template
    assert "#set sampling.temperature unset" in template


def test_secret_values_are_masked_when_shown():
    live_settings = settings.seed_defaults()

    changed = setcmd.run_repl_command(live_settings, "/set provider.api_key sk-test")
    shown = setcmd.run_repl_command(live_settings, "/show provider.api_key")

    assert changed.error is None
    assert changed.lines == ["provider.api_key = <set>"]
    assert settings.get_dotted(live_settings, ("provider", "api_key")) == "sk-test"
    assert shown.lines == ["provider.api_key = <set>"]


def test_unknown_knob_returns_error_without_mutating_settings():
    live_settings = settings.seed_defaults()
    before = copy.deepcopy(live_settings)

    result = setcmd.run_repl_command(live_settings, "/set missing.knob value")

    assert result.handled is True
    assert result.changed is False
    assert result.error == "unknown knob: missing.knob"
    assert live_settings == before


def test_map_sub_key_updates_parent_map_and_shows_parent():
    live_settings = settings.seed_defaults()

    changed = setcmd.run_repl_command(live_settings, "/set wiki.aliases.creative /p")
    shown = setcmd.run_repl_command(live_settings, "/show wiki.aliases")

    assert changed.error is None
    assert changed.lines == ["wiki.aliases.creative = /p"]
    assert settings.get_dotted(live_settings, ("wiki", "aliases", "creative")) == "/p"
    assert shown.error is None
    assert shown.lines == ["wiki.aliases = creative=/p"]


@pytest.mark.parametrize("line", ["show model.id", "run something"])
def test_apply_config_line_rejects_non_set_verbs(line: str):
    result = setcmd.apply_config_line(settings.seed_defaults(), line)

    assert result.handled is True
    assert result.changed is False
    assert result.error == f"unknown command: {line.split(maxsplit=1)[0]}"


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
    if spec.type == "bool":
        return "on", True
    if spec.type == "int":
        return "123", 123
    if spec.type == "float":
        return "0.25", 0.25
    if spec.type in {"json", "map"}:
        return '{"env": true}', {"env": True}
    return f"env-{spec.key}", f"env-{spec.key}"
