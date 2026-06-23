"""Tests for the CLI ``--extra KEY=VALUE`` one-shot config override.

``--extra`` sets a dotted config key for one run. It sits at the TOP of the
precedence stack (built-in defaults < jsrc files < env vars < --extra; see
``js/settings.py:15`` and ``js/settings.py:57``), so it wins over both env and
the jsrc files. The right-hand side is coerced int -> float -> bool/null -> str
by ``js.settings.coerce_extra_value`` (``js/settings.py:321``).

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


def test_parse_extra_arg_coerces_float_then_bool_then_string():
    # int beats float beats bool/null beats str (js/settings.py:321).
    assert settings.parse_extra_arg("compact.chars_per_token=4.5")[1] == 4.5
    assert settings.parse_extra_arg("runtime.trace=off")[1] is False
    assert settings.parse_extra_arg("runtime.debug=on")[1] is True
    assert settings.parse_extra_arg("provider.id=null")[1] is None
    assert settings.parse_extra_arg("model.id=some-model")[1] == "some-model"


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
