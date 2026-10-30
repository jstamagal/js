"""`js --migrate-config`: one-shot legacy config.toml -> jsrc set-script."""

from __future__ import annotations

import pytest

from js import cli


def test_migrate_no_legacy_returns_1(tmp_path, monkeypatch):
    monkeypatch.setattr(cli._paths, "legacy_global_config_file", lambda: tmp_path / "missing.toml")
    monkeypatch.setattr(cli._paths, "global_config_file", lambda: tmp_path / "jsrc")
    assert cli._run_migrate_config() == 1


# --- a jsrc file is rewritten ---

def _jsrc(text: str):
    from js.rename import rename_old_names

    return rename_old_names(text)


@pytest.mark.parametrize("old, new", [
    ("SET history.cwd_first on", "SET HISTORY_ORDER cwd"),
    ("alias hc set history.cwd_first off", "alias hc set HISTORY_ORDER last"),
    ("set provider.extra.thinking.type enabled", "set EXTRA_PARAMS.thinking.type enabled"),
    ("set history.cwd_first 'off'", "set HISTORY_ORDER 'last'"),
    ("set compact.context_window_overrides.openai/gpt-6 128000", "set CONTEXT_SIZE_PER_MODEL.openai/gpt-6 128000"),
    ("#   example:  set model.id cpa/x", "#   example:  set MODEL cpa/x"),
])
def test_a_jsrc_line_changes_only_its_setting_name(old, new):
    assert _jsrc(old) == (new, [])


@pytest.mark.parametrize("line, new", [
    ("set history.file /tmp/history.file", "set HISTORY_FILE /tmp/history.file"),
    ("set API_KEY sk-JS_TEMP=x", "set API_KEY sk-JS_TEMP=x"),
    ('set provider.extra {"meta":"JS_AGENT"}', 'set EXTRA_PARAMS {"meta":"JS_AGENT"}'),
])
def test_a_value_holding_an_old_name_is_kept_and_reported(line, new):
    assert _jsrc(line) == (new, [(1, "")])


def test_a_deleted_setting_is_commented_out_and_reported():
    assert _jsrc("set compact.context_window 4096") == ("#set compact.context_window 4096", [(1, " (setting deleted)")])
    assert _jsrc("on turn_start set limits.max_bash_output_ceiling 9")[1] == [(1, " (setting deleted)")]


# --- any other file is only reported on ---


def test_no_old_env_name_is_an_env_name_js_reads_now():
    from js import settings
    from js.rename import RENAMED_ENV

    current = {settings.canonical_env_name(spec.key) for spec in settings.REGISTRY}
    assert sorted(current & set(RENAMED_ENV)) == []


# ---------------------------------------------------------------------------
# A jsrc that still holds old names is rewritten once when js loads it
# ---------------------------------------------------------------------------

_OLD_JSRC = "set model.id old/model\nset limits.max_read_lines 77\n"


def _load(path):
    from js import settings

    loaded = settings.seed_defaults()
    return loaded, settings.load_jsrc_files([path], loaded)


def test_an_old_names_jsrc_loads_with_the_new_names_and_no_copy(tmp_path):
    from js import settings

    rc = tmp_path / "rc"
    rc.mkdir()
    jsrc = rc / "jsrc"
    jsrc.write_text(_OLD_JSRC, encoding="utf-8")

    loaded, warnings = _load(jsrc)

    assert warnings == []
    assert settings.knob(loaded, "MODEL") == "old/model"
    assert settings.knob(loaded, "READ_LINES_MAX") == 77
    assert jsrc.read_text(encoding="utf-8") == "set MODEL old/model\nset READ_LINES_MAX 77\n"
    assert list(rc.iterdir()) == [jsrc]


def test_a_second_load_changes_nothing(tmp_path):
    rc = tmp_path / "rc"
    rc.mkdir()
    jsrc = rc / "jsrc"
    jsrc.write_text(_OLD_JSRC, encoding="utf-8")
    _load(jsrc)
    after = {path: path.read_text(encoding="utf-8") for path in rc.iterdir()}

    _load(jsrc)

    assert {path: path.read_text(encoding="utf-8") for path in rc.iterdir()} == after


def test_a_new_names_jsrc_that_mentions_an_old_name_is_left_as_it_is(tmp_path):
    from js import settings

    rc = tmp_path / "rc"
    rc.mkdir()
    jsrc = rc / "jsrc"
    text = 'set SHELL_ENV_KEEP PATH,JS_TEMP\nset BANNER "was model.id"\nalias m echo agent=forge\n'
    jsrc.write_text(text, encoding="utf-8")

    loaded, _warnings = _load(jsrc)

    assert jsrc.read_bytes() == text.encode("utf-8")
    assert list(rc.iterdir()) == [jsrc]
    assert settings.knob(loaded, "BANNER") == "was model.id"


def test_migration_through_a_symlink_keeps_the_config_shared(tmp_path):
    from js import rename, settings

    shared = tmp_path / "shared" / "jsrc"
    shared.parent.mkdir()
    shared.write_text("set model.id old/model\n", encoding="utf-8")
    linked = tmp_path / "project" / "jsrc"
    linked.parent.mkdir()
    linked.symlink_to(shared)

    assert rename.migrate_jsrc_file(linked)

    assert linked.is_symlink()
    assert shared.read_text() == "set MODEL old/model\n"
    shared.write_text("set MODEL new/model\n", encoding="utf-8")
    assert settings.knob(_load(linked)[0], "MODEL") == "new/model"
