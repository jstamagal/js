"""One render step and its targets: a format becomes colour runs, and each
output target (ANSI, prompt_toolkit, rich, plain) is drawn from them, with
NO_COLOR applied by the targets."""

from __future__ import annotations

import io
import sys

from js import cli, fset, screen, setcmd, settings
from js import messages as msgs



def test_a_colour_set_in_the_format_beats_the_severity_paint():
    fset.set_entry("SAVE_FAILED", "%C03%s%C")
    line = msgs.SAVE_FAILED.line(error="disk full")
    assert f"{fset.fg('PALETTE_03')}disk full" in line
    assert fset.fg("COLOR_ERROR") not in line


def test_no_color_takes_colour_off_the_screen_and_the_status_bar(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    assert "#" not in screen.screen_style() and "#" not in screen.status_style()
    assert all(not style for style, _text in fset.to_fragments(msgs.STATUS_CACHE.runs(pct=50)))


class _Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_a_settings_confirmation_keeps_its_format_colour_on_a_terminal(monkeypatch):
    monkeypatch.setattr(sys, "stdout", _Terminal())
    fset.set_entry("SETTING_CHANGED", "%C04%s = %s")
    result = setcmd.set_command(settings.seed_defaults(), "TRANSCRIPT_LOG off")
    assert result.lines and fset.fg("PALETTE_04") in result.lines[0]


def test_an_fset_refusal_speaks_through_its_format(monkeypatch, capsys):
    fset.set_entry("FSET_UNKNOWN", "NO SUCH ENTRY %s")
    state = {"messages": [], "system": "s", "settings": settings.seed_defaults()}
    cli._handle_command("/fset NOT_AN_ENTRY", state, type("Cfg", (), {"settings": None})())
    assert "NO SUCH ENTRY NOT_AN_ENTRY" in fset.plain(capsys.readouterr().out)


def test_the_startup_run_of_the_jsrc_leaves_a_model_flag_in_charge(monkeypatch, tmp_path):
    from js.config import from_env, jsrc_paths
    from repl_driver import repl_state

    (tmp_path / ".js").mkdir()
    (tmp_path / ".js" / "jsrc").write_text("set MODEL from-jsrc\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    cfg = from_env()
    state, _spec = repl_state(cfg, model="from-flag")
    cli._run_rc_commands(state, cfg, jsrc_paths(tmp_path))
    assert state["model"] == "from-flag"


def test_the_startup_run_of_the_jsrc_leaves_a_resumed_session_s_effort_in_charge(monkeypatch, tmp_path):
    from js.config import from_env, jsrc_paths
    from repl_driver import repl_state

    (tmp_path / ".js").mkdir()
    (tmp_path / ".js" / "jsrc").write_text("set EFFORT high\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    cfg = from_env()
    state, _spec = repl_state(cfg)
    state["live_resume"] = {"EFFORT": "low"}
    cli._run_rc_commands(state, cfg, jsrc_paths(tmp_path))
    assert cli._cfg_for_live_state(cfg, state).reasoning_effort == "low"


def test_a_setting_s_short_name_in_the_jsrc_sets_it_at_startup_and_switches_nothing(monkeypatch, tmp_path):
    from js.config import from_env, jsrc_paths
    from repl_driver import repl_state

    (tmp_path / ".js").mkdir()
    (tmp_path / ".js" / "jsrc").write_text("model from-jsrc\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    cfg = from_env()
    state, _spec = repl_state(cfg, model="from-flag")
    assert cli._run_rc_commands(state, cfg, jsrc_paths(tmp_path)) == []
    assert state["model"] == "from-flag"


def test_a_dollar_name_of_a_list_setting_expands_as_json(tmp_path):
    path = tmp_path / "jsrc"
    path.write_text('fset TOOLS_QUIET ["read"]\nfset TOOLS_QUIET $TOOLS_QUIET\n', encoding="utf-8")
    loaded = settings.seed_defaults()
    assert settings.load_jsrc_files([path], loaded) == []
    assert settings.knob(loaded, "TOOLS_QUIET") == ["read"]


def test_collecting_the_config_twice_does_not_compound_fset_entries(tmp_path):
    path = tmp_path / "jsrc"
    path.write_text("fset MARK_AGENT $MARK_AGENT>\n", encoding="utf-8")
    settings.collect_settings(config_paths=[path], env={})
    settings.collect_settings(config_paths=[path], env={})
    assert fset.lookup("MARK_AGENT") == "●>"


def test_save_rewrites_every_live_line_of_an_entry(tmp_path):
    path = tmp_path / "jsrc"
    path.write_text("fset THEME crush\nfset THEME github\n", encoding="utf-8")
    fset.set_entry("THEME", "paper")
    settings.save_settings_to_jsrc(path, settings.seed_defaults())
    fset.reset()
    settings.load_jsrc_files([path], settings.seed_defaults())
    assert fset.theme_name() == "paper"
