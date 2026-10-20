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
