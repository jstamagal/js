"""`js --migrate-config`: one-shot legacy config.toml -> jsrc set-script."""

from __future__ import annotations

import pytest

from js import cli


def test_migrate_writes_jsrc_set_lines_from_toml(tmp_path, monkeypatch):
    legacy = tmp_path / "config.toml"
    target = tmp_path / "jsrc"
    legacy.write_text(
        '[model]\nid = "deepseek/deepseek-v4-flash"\n[limits]\nmax_tool_iterations = 50\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(cli._paths, "legacy_global_config_file", lambda: legacy)
    monkeypatch.setattr(cli._paths, "global_config_file", lambda: target)

    assert cli._run_migrate_config() == 0
    text = target.read_text(encoding="utf-8")
    assert "set MODEL deepseek/deepseek-v4-flash" in text
    assert "set TURN_ROUNDS_MAX 50" in text


def test_migrate_no_legacy_returns_1(tmp_path, monkeypatch):
    monkeypatch.setattr(cli._paths, "legacy_global_config_file", lambda: tmp_path / "missing.toml")
    monkeypatch.setattr(cli._paths, "global_config_file", lambda: tmp_path / "jsrc")
    assert cli._run_migrate_config() == 1


def test_migrate_refuses_when_target_exists(tmp_path, monkeypatch):
    legacy = tmp_path / "config.toml"
    legacy.write_text('[model]\nid = "x"\n', encoding="utf-8")
    target = tmp_path / "jsrc"
    target.write_text("set MODEL x\n", encoding="utf-8")
    monkeypatch.setattr(cli._paths, "legacy_global_config_file", lambda: legacy)
    monkeypatch.setattr(cli._paths, "global_config_file", lambda: target)
    assert cli._run_migrate_config() == 1


def test_migrate_turns_cwd_first_into_a_history_order(tmp_path, monkeypatch):
    legacy = tmp_path / "config.toml"
    target = tmp_path / "jsrc"
    legacy.write_text("[history]\ncwd_first = true\n", encoding="utf-8")
    monkeypatch.setattr(cli._paths, "legacy_global_config_file", lambda: legacy)
    monkeypatch.setattr(cli._paths, "global_config_file", lambda: target)
    assert cli._run_migrate_config() == 0
    assert "set HISTORY_ORDER cwd" in target.read_text(encoding="utf-8")



def test_migrate_renames_children_of_a_map_setting(tmp_path, monkeypatch):
    legacy = tmp_path / "config.toml"
    target = tmp_path / "jsrc"
    legacy.write_text('[provider.extra.thinking]\ntype = "enabled"\n'
                      '[compact.context_window_overrides]\nmy-model = 4096\n', encoding="utf-8")
    monkeypatch.setattr(cli._paths, "legacy_global_config_file", lambda: legacy)
    monkeypatch.setattr(cli._paths, "global_config_file", lambda: target)
    assert cli._run_migrate_config() == 0
    text = target.read_text(encoding="utf-8")
    assert "set EXTRA_PARAMS.thinking.type enabled" in text
    assert "set CONTEXT_SIZE_PER_MODEL.my-model 4096" in text


def test_migrate_comments_out_a_deleted_setting_and_says_so(tmp_path, monkeypatch, capsys):
    legacy = tmp_path / "config.toml"
    target = tmp_path / "jsrc"
    legacy.write_text("[compact]\ncontext_window = 4096\n", encoding="utf-8")
    monkeypatch.setattr(cli._paths, "legacy_global_config_file", lambda: legacy)
    monkeypatch.setattr(cli._paths, "global_config_file", lambda: target)
    assert cli._run_migrate_config() == 1
    text = target.read_text(encoding="utf-8")
    assert "# deleted: set compact.context_window 4096" in text
    assert not any(line.startswith("set compact.") for line in text.splitlines())
    assert "compact.context_window" in capsys.readouterr().err



# --- a jsrc file is rewritten ---

def _jsrc(text: str):
    from js.rename import rename_old_names

    return rename_old_names(text)


def test_renaming_a_jsrc_converts_cwd_first_values():
    assert _jsrc("set history.cwd_first on\n#set history.cwd_first off\n") == (
        "set HISTORY_ORDER cwd\n#set HISTORY_ORDER last\n", [])


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


@pytest.mark.parametrize("line", ["set history.cwd_first $X", "set history.cwd_first `cat flag`"])
def test_a_history_value_that_is_not_a_boolean_is_left_and_reported(line):
    assert _jsrc(line) == (line, [(1, "")])


def test_a_deleted_setting_is_commented_out_and_reported():
    assert _jsrc("set compact.context_window 4096") == ("#set compact.context_window 4096", [(1, " (setting deleted)")])
    assert _jsrc("on turn_start set limits.max_bash_output_ceiling 9")[1] == [(1, " (setting deleted)")]


def test_anything_old_left_on_a_jsrc_line_is_reported():
    _new, left = _jsrc("# see model.id\nset tags.max 3\nset READ_LINES_MAX 9\n")
    assert left == [(1, ""), (2, " (setting deleted)")]


def test_two_spellings_of_one_setting_are_reported():
    assert _jsrc("set EFFORT low\nset model.reasoning_effort high")[1] == [(1, " (set twice)"), (2, " (set twice)")]
    assert _jsrc("set EFFORT low\nset EFFORT high")[1] == []


def test_the_env_var_of_a_setting_that_became_a_colour_entry_is_reported_gone():
    from js.rename import report_old_names

    assert report_old_names("export JS_THEME_ACCENT=#ff0000") == [(1, "JS_THEME_ACCENT", None)]


def test_kind_of_rewrites_only_jsrc_files():
    from js.rename import kind_of

    assert [kind_of("/home/u/.js/jsrc"), kind_of("p/.js/jsrc.local"), kind_of("tools.irc"),
            kind_of("bin/fastjs"), kind_of("x.py")] == ["jsrc", "jsrc", "jsrc", "other", "other"]


# --- any other file is only reported on ---

def test_a_shell_or_python_file_is_unchanged_and_every_old_name_is_reported(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    script = tmp_path / "run.sh"
    script.write_text('export JS_REASONING=high\njs --extra limits.max_read_lines=7 -p "$JS_TEMP"\n', encoding="utf-8")
    program = tmp_path / "drive.py"
    program.write_text('os.environ["JS_AGENT"] = "x"\nargs = ["--extra", "compact.context_window=9"]\n',
                       encoding="utf-8")
    before = script.read_text(), program.read_text()
    tool = Path(__file__).resolve().parent.parent / "scripts" / "rename-settings.py"
    run = subprocess.run([sys.executable, str(tool), "--write", str(script), str(program)],
                         capture_output=True, text=True)
    assert run.returncode == 1
    assert (script.read_text(), program.read_text()) == before
    assert run.stdout.splitlines() == [
        f"{script}:1: old name JS_REASONING -> JS_EFFORT, edit by hand",
        f"{script}:2: old name limits.max_read_lines -> READ_LINES_MAX, edit by hand",
        f"{script}:2: old name JS_TEMP -> JS_TEMPERATURE, edit by hand",
        f"{program}:1: old name JS_AGENT -> JS_DEFAULT_AGENT, edit by hand",
        f"{program}:2: old name compact.context_window (setting deleted), edit by hand",
    ]


def test_old_canonical_env_names_are_known():
    from js.rename import report_old_names

    assert report_old_names("export JS_COMPACT_AUTO=off JS_LIMITS_MAX_BASH_OUTPUT_CEILING=1") == [
        (1, "JS_COMPACT_AUTO", "JS_COMPACTION_AUTO"), (1, "JS_LIMITS_MAX_BASH_OUTPUT_CEILING", None)]


def test_no_old_env_name_is_an_env_name_js_reads_now():
    from js import settings
    from js.rename import RENAMED_ENV

    current = {settings.canonical_env_name(spec.key) for spec in settings.REGISTRY}
    assert sorted(current & set(RENAMED_ENV)) == []


@pytest.mark.parametrize("line, new", [
    ("alias clip set ui.paste_image_command printf --extra model.id=literal",
     "alias clip set PASTE_IMAGE_COMMAND printf --extra model.id=literal"),
    ("alias two exec a --extra model.id=x; set MODEL y", "alias two exec a --extra model.id=x; set MODEL y"),
])
def test_an_extra_inside_a_set_value_or_a_mixed_body_is_left_and_reported(line, new):
    assert _jsrc(line) == (new, [(1, "")])


def test_a_quoted_agent_is_reported_in_a_program():
    from js.rename import report_old_names

    assert report_old_names('knob(opts, "agent")\nx = {\'agent\': 1}\nthe agent runs') == [
        (1, "agent", "DEFAULT_AGENT"), (2, "agent", "DEFAULT_AGENT")]


@pytest.mark.parametrize("line", [
    "alias x exec js --extra limits.max_read_lines=7",
    "on turn_start true;echo --extra model.id=x",
    "alias y set model.id x;rm",
    "alias z set model.id $(cat f)",
    "alias e exec js --extra model\\.id=x",
    "alias q exec js --extra ag'ent'=forge",
])
def test_a_body_that_is_not_one_plain_set_is_left_and_reported(line):
    assert _jsrc(line) == (line, [(1, "")])


def test_an_escaped_name_in_a_shell_file_is_reported():
    from js.rename import report_old_names

    assert report_old_names("js --extra model\\.id=x") == [(1, "model.id", "MODEL")]


def test_an_apostrophe_in_a_comment_does_not_hide_an_old_name():
    line = "alias a exec js --extra model\\.id=x # owner's comment"
    assert _jsrc(line) == (line, [(1, "")])


def test_a_quoted_agent_key_in_a_jsrc_body_is_reported():
    line = """on turn_start exec python -c 'print(SPEC_BY_KEY["agent"])'"""
    assert _jsrc(line) == (line, [(1, "")])


@pytest.mark.parametrize("line", ["alias a set model.id x >/tmp/out", "alias b set model.id x </dev/null"])
def test_a_body_with_a_redirection_is_left_and_reported(line):
    assert _jsrc(line) == (line, [(1, "")])


def test_jsrc_shell_and_python_reports_come_from_one_reporter(monkeypatch):
    from js import rename

    monkeypatch.setattr(rename, "line_report", lambda line: [("OLD", None)] if "x" in line else [])
    assert rename.rename_old_names("x") == ("x", [(1, " (setting deleted)")])
    assert rename.report_old_names("x\ny") == [(1, "OLD", None)]


def test_a_name_split_by_quotes_and_backslashes_is_reported():
    line = "alias a exec js --extra model'.'\\id=x # owner's comment"
    assert _jsrc(line) == (line, [(1, "")])


@pytest.mark.parametrize("text", ['run("agent =forge")', 'dict(agent = "forge")'])
def test_spaces_around_an_equals_sign_do_not_hide_agent(text):
    from js.rename import report_old_names

    assert report_old_names(text) == [(1, "agent", "DEFAULT_AGENT")]


def test_an_exec_body_in_a_jsrc_is_reported():
    line = "on turn_start exec sh -c 'JS_REASONING=high js -p hi'"
    assert _jsrc(line) == (line, [(1, "")])


def test_migrate_writes_display_and_colour_settings_as_fset_lines_that_reload(tmp_path, monkeypatch):
    from js import fset, settings

    legacy = tmp_path / "config.toml"
    target = tmp_path / "jsrc"
    legacy.write_text('[ui]\ntools = 3\n[theme]\naccent = "#112233"\n', encoding="utf-8")
    monkeypatch.setattr(cli._paths, "legacy_global_config_file", lambda: legacy)
    monkeypatch.setattr(cli._paths, "global_config_file", lambda: target)

    assert cli._run_migrate_config() == 0
    loaded = settings.seed_defaults()
    assert settings.load_jsrc_files([target], loaded) == []
    assert settings.knob(loaded, "TOOLS_DISPLAY") == 3
    assert fset.hex_for("COLOR_TOOL") == "#112233"


def test_a_migrated_value_keeps_a_literal_dollar(tmp_path, monkeypatch):
    from js import settings

    legacy = tmp_path / "config.toml"
    target = tmp_path / "jsrc"
    legacy.write_text('BANNER = "$MODEL"\n', encoding="utf-8")
    monkeypatch.setattr(cli._paths, "legacy_global_config_file", lambda: legacy)
    monkeypatch.setattr(cli._paths, "global_config_file", lambda: target)

    assert cli._run_migrate_config() == 0
    loaded = settings.seed_defaults()
    settings.load_jsrc_files([target], loaded)
    assert settings.knob(loaded, "BANNER") == "$MODEL"


def test_a_migrated_map_child_keeps_its_quotes_as_they_were(tmp_path, monkeypatch):
    from js import settings

    legacy = tmp_path / "config.toml"
    target = tmp_path / "jsrc"
    legacy.write_text('[provider.extra]\nnote = "\\"quoted\\" text"\n', encoding="utf-8")
    monkeypatch.setattr(cli._paths, "legacy_global_config_file", lambda: legacy)
    monkeypatch.setattr(cli._paths, "global_config_file", lambda: target)

    assert cli._run_migrate_config() == 0
    loaded = settings.seed_defaults()
    settings.load_jsrc_files([target], loaded)
    assert settings.knob(loaded, "EXTRA_PARAMS")["note"] == '"quoted" text'


# ---------------------------------------------------------------------------
# A jsrc that still holds old names is rewritten once when js loads it
# ---------------------------------------------------------------------------

_OLD_JSRC = "set model.id old/model\nset limits.max_read_lines 77\n"


def _load(path):
    from js import settings

    loaded = settings.seed_defaults()
    return loaded, settings.load_jsrc_files([path], loaded)


def test_an_old_names_jsrc_loads_with_the_new_names_and_a_backup(tmp_path):
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
    backups = [path for path in rc.iterdir() if path != jsrc]
    assert [path.read_text(encoding="utf-8") for path in backups] == [_OLD_JSRC]


def test_a_second_load_changes_nothing(tmp_path):
    rc = tmp_path / "rc"
    rc.mkdir()
    jsrc = rc / "jsrc"
    jsrc.write_text(_OLD_JSRC, encoding="utf-8")
    _load(jsrc)
    after = {path: path.read_text(encoding="utf-8") for path in rc.iterdir()}

    _load(jsrc)

    assert {path: path.read_text(encoding="utf-8") for path in rc.iterdir()} == after


def test_an_existing_backup_is_kept(tmp_path):
    rc = tmp_path / "rc"
    rc.mkdir()
    jsrc = rc / "jsrc"
    jsrc.write_text(_OLD_JSRC, encoding="utf-8")
    _load(jsrc)
    first = {path: path.read_text(encoding="utf-8") for path in rc.iterdir() if path != jsrc}
    jsrc.write_text("set agent forge\n", encoding="utf-8")

    _load(jsrc)

    assert all(path.read_text(encoding="utf-8") == text for path, text in first.items())
    assert len(list(rc.iterdir())) == 3


def test_an_unconvertible_live_line_is_commented_out_and_named(tmp_path, capsys):
    from js.rename import NOT_MIGRATED

    rc = tmp_path / "rc"
    rc.mkdir()
    jsrc = rc / "jsrc"
    bad = "alias a set model.id x >/tmp/out"
    jsrc.write_text(f"set model.id m\n{bad}\n", encoding="utf-8")

    _loaded, warnings = _load(jsrc)

    assert warnings == []
    lines = jsrc.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "set MODEL m"
    assert lines[1].startswith(NOT_MIGRATED) and lines[1].endswith(bad)
    assert f"{jsrc}:2" in capsys.readouterr().err


@pytest.mark.skipif(__import__("os").geteuid() == 0, reason="root writes a read-only file")
def test_a_read_only_jsrc_is_left_alone_with_a_warning(tmp_path, capsys):
    rc = tmp_path / "rc"
    rc.mkdir()
    jsrc = rc / "jsrc"
    jsrc.write_text(_OLD_JSRC, encoding="utf-8")
    jsrc.chmod(0o444)

    _load(jsrc)

    assert jsrc.read_text(encoding="utf-8") == _OLD_JSRC
    assert list(rc.iterdir()) == [jsrc]
    assert "rename-settings.py --write" in capsys.readouterr().err


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


def test_a_name_set_from_both_spellings_keeps_the_new_spelling_live(tmp_path):
    from js import settings

    rc = tmp_path / "rc"
    rc.mkdir()
    jsrc = rc / "jsrc"
    jsrc.write_text("set MODEL new/model\nset model.id old/model\n", encoding="utf-8")

    loaded, _warnings = _load(jsrc)

    assert settings.knob(loaded, "MODEL") == "new/model"


def test_a_backup_name_taken_by_another_start_still_migrates(tmp_path, monkeypatch):
    import datetime

    from js import rename

    rc = tmp_path / "rc"
    rc.mkdir()
    jsrc = rc / "jsrc"
    jsrc.write_text(_OLD_JSRC, encoding="utf-8")
    taken = rc / f"jsrc.bak-{datetime.date.today().isoformat()}"
    taken.write_text("another start's backup\n", encoding="utf-8")
    # Another js start creates the name between the check and the create.
    monkeypatch.setattr(rename.os.path, "lexists", lambda _path: False)

    _load(jsrc)

    assert jsrc.read_text(encoding="utf-8") == "set MODEL old/model\nset READ_LINES_MAX 77\n"
    assert taken.read_text(encoding="utf-8") == "another start's backup\n"
