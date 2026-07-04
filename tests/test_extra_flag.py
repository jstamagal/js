"""Tests for the CLI ``--extra KEY=VALUE`` one-shot config override.

``--extra`` sets a dotted config key for one run. It sits at the TOP of the
precedence stack (built-in defaults < jsrc files < env vars < --extra; see
``js/settings.py:15`` and ``js/settings.py:57``), so it wins over both env and
the jsrc files. Most right-hand sides are coerced int -> float -> bool/null ->
str by ``js.settings.coerce_extra_value``; exact registered keys use shared
setting coercion so ``--extra`` matches ``set`` and env/default loading.

These exercise the real load path: the focused ``settings.collect_settings`` /
``settings.parse_extra_arg`` unit and the integrated ``js.config.from_env``
(the same call ``js/cli.py`` makes with ``args.extras``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from js import settings
from js.config import from_env


def _env_dirs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    config_home = tmp_path / "config"
    data_home = tmp_path / "data"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    for spec in settings.REGISTRY:
        if spec.env:
            monkeypatch.delenv(spec.env, raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    return config_home, data_home


# ---------------------------------------------------------------------------
# parse_extra_arg: dotted key + type coercion (js/settings.py:342)
# ---------------------------------------------------------------------------

def test_parse_extra_arg_splits_dotted_key_and_coerces_int():
    path, value = settings.parse_extra_arg("limits.task_max_depth=3")

    assert path == ("limits", "task_max_depth")
    assert value == 3
    assert isinstance(value, int)


def test_parse_extra_arg_coerces_registered_values():
    # Exact registered keys use registry coercion, including string/null semantics.
    assert settings.parse_extra_arg("compact.chars_per_token=4.5")[1] == 4.5
    assert settings.parse_extra_arg("runtime.trace=off")[1] is False
    assert settings.parse_extra_arg("runtime.debug=on")[1] is True
    assert settings.parse_extra_arg("provider.id=null")[1] == "null"
    assert settings.parse_extra_arg("model.id=some-model")[1] == "some-model"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("model.id=123", "123"),
        # RULING A: magic strings die -- a str knob stores "off" verbatim now;
        # `set -provider.id` / `set -compact.pre_hook` is the only clear path.
        ("provider.id=off", "off"),
        ("compact.pre_hook=off", "off"),
    ],
)
def test_extra_registered_string_keys_use_registry_coercion(raw, expected):
    assert settings.parse_extra_arg(raw)[1] == expected


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("runtime.trace=maybe", "expected on/off"),
        ("limits.fetch_timeout_s=abc", "expected an integer"),
        ("compact.notify_threshold=abc", "expected a number"),
    ],
)
def test_extra_registered_scalar_keys_use_registry_validation(raw, message):
    with pytest.raises(ValueError, match=f"--extra .*: {message}"):
        settings.parse_extra_arg(raw)


def test_extra_reasoning_effort_off_disables_via_registry(monkeypatch, tmp_path):
    # RULING B: `off` is the only token that disables reasoning now.
    _env_dirs(monkeypatch, tmp_path)

    path, value = settings.parse_extra_arg("model.reasoning_effort=off")
    cfg = from_env(save_session=False, extras=["model.reasoning_effort=off"])

    assert path == ("model", "reasoning_effort")
    assert value == "none"
    assert cfg.reasoning_effort == "none"


@pytest.mark.parametrize("alias", ["none", "0", "default", "auto", "unset"])
def test_extra_reasoning_effort_rejects_former_null_tokens(alias):
    # RULING B: only off|minimal|low|medium|high|xhigh|max are valid; the old
    # none/0/default/auto/unset synonyms are now a clean rejection, not a
    # silent re-enable of the provider default.
    with pytest.raises(
        ValueError,
        match=r"--extra model\.reasoning_effort: expected off\|minimal\|low\|medium\|high\|xhigh\|max",
    ):
        settings.parse_extra_arg(f"model.reasoning_effort={alias}")


def test_extra_provider_extra_json_uses_registry_map_coercion(monkeypatch, tmp_path):
    _env_dirs(monkeypatch, tmp_path)
    raw = 'provider.extra={"extra_body":{"probe":true},"mode":"fast"}'
    expected = {"extra_body": {"probe": True}, "mode": "fast"}

    path, value = settings.parse_extra_arg(raw)
    cfg = from_env(save_session=False, extras=[raw])

    assert path == ("provider", "extra")
    assert value == expected
    assert settings.get_dotted(cfg.settings, ("provider", "extra")) == expected


def test_extra_provider_extra_rejects_non_object_json():
    raw = 'provider.extra=["extra_body"]'

    with pytest.raises(ValueError, match="--extra provider\\.extra: expected a JSON object"):
        settings.parse_extra_arg(raw)


def test_extra_provider_extra_subkeys_use_generic_coercion(monkeypatch, tmp_path):
    _env_dirs(monkeypatch, tmp_path)
    raw = "provider.extra.live_flag=true"

    path, value = settings.parse_extra_arg(raw)
    cfg = from_env(save_session=False, extras=[raw])

    assert path == ("provider", "extra", "live_flag")
    assert value is True
    assert settings.get_dotted(cfg.settings, ("provider", "extra", "live_flag")) is True


def test_extra_wiki_aliases_map_uses_registry_coercion(monkeypatch, tmp_path):
    _env_dirs(monkeypatch, tmp_path)
    raw = 'wiki.aliases={"creative":"~/wiki-creative"}'
    expected = {"creative": "~/wiki-creative"}

    path, value = settings.parse_extra_arg(raw)
    cfg = from_env(save_session=False, extras=[raw])

    assert path == ("wiki", "aliases")
    assert value == expected
    assert settings.get_dotted(cfg.settings, ("wiki", "aliases")) == expected


def test_extra_tools_alias_profiles_json_uses_registry_coercion(monkeypatch, tmp_path):
    _env_dirs(monkeypatch, tmp_path)
    raw = 'tools.alias_profiles=[{"match":["offline-test-model"],"aliases":{"read":"r"}}]'
    expected = [{"match": ["offline-test-model"], "aliases": {"read": "r"}}]

    path, value = settings.parse_extra_arg(raw)
    cfg = from_env(save_session=False, extras=[raw])

    assert path == ("tools", "alias_profiles")
    assert value == expected
    assert settings.get_dotted(cfg.settings, ("tools", "alias_profiles")) == expected


def test_extra_tools_alias_profiles_accepts_string_match(monkeypatch, tmp_path):
    _env_dirs(monkeypatch, tmp_path)
    raw = 'tools.alias_profiles=[{"match":"offline-test-model","aliases":{"read":"r"}}]'
    expected = [{"match": "offline-test-model", "aliases": {"read": "r"}}]

    path, value = settings.parse_extra_arg(raw)
    cfg = from_env(save_session=False, extras=[raw])

    assert path == ("tools", "alias_profiles")
    assert value == expected
    assert settings.get_dotted(cfg.settings, ("tools", "alias_profiles")) == expected


def test_extra_tools_alias_profiles_rejects_non_list_json():
    raw = 'tools.alias_profiles={"match":["offline-test-model"],"aliases":{"read":"r"}}'

    with pytest.raises(ValueError, match="--extra tools\\.alias_profiles: expected a JSON list"):
        settings.parse_extra_arg(raw)


def test_extra_tools_alias_profiles_rejects_entries_without_aliases():
    raw = 'tools.alias_profiles=[{"match":["offline-test-model"]}]'

    with pytest.raises(
        ValueError,
        match="--extra tools\\.alias_profiles: expected profiles with match and aliases",
    ):
        settings.parse_extra_arg(raw)


def test_extra_tools_alias_profiles_rejects_empty_alias_maps():
    raw = 'tools.alias_profiles=[{"match":["offline-test-model"],"aliases":{}}]'

    with pytest.raises(
        ValueError,
        match="--extra tools\\.alias_profiles: expected non-empty aliases",
    ):
        settings.parse_extra_arg(raw)


def test_extra_tools_alias_profiles_rejects_empty_match_values():
    raw = 'tools.alias_profiles=[{"match":[],"aliases":{"read":"Read"}}]'

    with pytest.raises(
        ValueError,
        match="--extra tools\\.alias_profiles: expected non-empty match values",
    ):
        settings.parse_extra_arg(raw)


def test_extra_tools_alias_profiles_rejects_duplicate_alias_names():
    raw = 'tools.alias_profiles=[{"match":["offline-test-model"],"aliases":{"read":"Tool","write":"tool"}}]'

    with pytest.raises(
        ValueError,
        match="--extra tools\\.alias_profiles: expected unique alias names",
    ):
        settings.parse_extra_arg(raw)


def test_extra_tools_alias_profiles_rejects_invalid_alias_names():
    raw = 'tools.alias_profiles=[{"match":["offline-test-model"],"aliases":{"read":"read file"}}]'

    with pytest.raises(
        ValueError,
        match="--extra tools\\.alias_profiles: expected alias names matching \\[A-Za-z0-9_-\\]\\+",
    ):
        settings.parse_extra_arg(raw)


def test_extra_tools_alias_profiles_rejects_invalid_canonical_names():
    raw = 'tools.alias_profiles=[{"match":["offline-test-model"],"aliases":{"read file":"Read"}}]'

    with pytest.raises(
        ValueError,
        match=(
            "--extra tools\\.alias_profiles: "
            "expected canonical tool names matching \\[A-Za-z0-9_-\\]\\+"
        ),
    ):
        settings.parse_extra_arg(raw)


@pytest.mark.parametrize("raw", ["tools.alias_profiles.foo=bar", "model.id.foo=bar"])
def test_extra_rejects_registered_non_map_subkeys(raw):
    with pytest.raises(ValueError, match="--extra unknown knob:"):
        settings.parse_extra_arg(raw)


def test_parse_extra_arg_rejects_missing_eq_and_empty_sides():
    with pytest.raises(ValueError):
        settings.parse_extra_arg("limits.task_max_depth")  # no '='
    with pytest.raises(ValueError):
        settings.parse_extra_arg("=3")  # empty key
    with pytest.raises(ValueError):
        settings.parse_extra_arg("limits.task_max_depth=")  # empty value


# ---------------------------------------------------------------------------
# collect_settings: --extra wins over env and jsrc, value lands at dotted path
# ---------------------------------------------------------------------------

def test_collect_settings_extra_beats_env_and_jsrc_with_int_coercion(tmp_path):
    cfg = tmp_path / "jsrc"
    cfg.write_text("set limits.task_max_depth 1\n", encoding="utf-8")

    out = settings.collect_settings(
        config_paths=[cfg],
        env={"JS_MAX_TOOL_ITERATIONS": "9"},  # unrelated env, just present
        extras=["limits.task_max_depth=3"],
    )

    # extra coerced str "3" -> int 3 and landed at the dotted path, beating jsrc.
    assert out["limits"]["task_max_depth"] == 3
    assert isinstance(out["limits"]["task_max_depth"], int)


def test_collect_settings_extra_string_beats_env_for_same_key(tmp_path):
    cfg = tmp_path / "jsrc"
    cfg.write_text("set model.id file-model\n", encoding="utf-8")

    out = settings.collect_settings(
        config_paths=[cfg],
        env={"JS_MODEL": "env-model"},
        extras=["model.id=cli-model"],
    )

    assert out["model"]["id"] == "cli-model"


def test_collect_settings_extra_registered_keys_use_registry_coercion(tmp_path):
    cfg = tmp_path / "jsrc"
    cfg.write_text("set model.id file-model\nset provider.id file-provider\n", encoding="utf-8")

    out = settings.collect_settings(
        config_paths=[cfg],
        env={"JS_MODEL": "env-model", "JS_PROVIDER": "env-provider"},
        extras=["model.id=123", "provider.id=off"],
    )

    assert out["model"]["id"] == "123"
    # RULING A: "off" is no longer a magic clear-token -- it's stored verbatim.
    assert out["provider"]["id"] == "off"


# ---------------------------------------------------------------------------
# from_env integration: the path js/cli.py actually drives (extras=args.extras)
# ---------------------------------------------------------------------------

def test_from_env_extra_wins_over_env_and_jsrc_for_one_run(monkeypatch, tmp_path):
    _env_dirs(monkeypatch, tmp_path)
    project = tmp_path / "project"
    cfg_path = project / ".js" / "jsrc"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text("set limits.task_max_depth 1\n", encoding="utf-8")
    monkeypatch.setenv("JS_MODEL", "env-model")

    # Baseline: with no --extra, env wins the model and jsrc wins task_max_depth.
    base = from_env(cwd=project, save_session=False)
    assert base.model == "env-model"
    assert base.task_max_depth == 1

    # --extra overrides both for this one run, with str -> int coercion.
    cfg = from_env(
        cwd=project,
        save_session=False,
        extras=["limits.task_max_depth=3", "model.id=cli-model"],
    )
    assert cfg.task_max_depth == 3
    assert isinstance(cfg.task_max_depth, int)
    assert cfg.model == "cli-model"


def test_from_env_extra_is_one_shot_not_persisted(monkeypatch, tmp_path):
    _env_dirs(monkeypatch, tmp_path)
    project = tmp_path / "project"
    cfg_path = project / ".js" / "jsrc"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text("set limits.task_max_depth 1\n", encoding="utf-8")

    with_extra = from_env(cwd=project, save_session=False, extras=["limits.task_max_depth=3"])
    assert with_extra.task_max_depth == 3

    # A subsequent run without the flag falls back to the jsrc value: not sticky.
    without_extra = from_env(cwd=project, save_session=False)
    assert without_extra.task_max_depth == 1
