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


def test_the_kernel_panel_draws_24_bit_colour_on_a_256_colour_terminal(monkeypatch, tmp_path):
    from js.toolkit import ToolContext
    from js.toolkit import kernel as kmod

    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("COLORTERM", raising=False)
    monkeypatch.setattr(sys, "stderr", _Terminal())
    fset.set_entry("COLOR_ERROR", "#123456")
    kmod.render_execution(ToolContext(cwd=tmp_path), level="quiet", code="x=1", stdout="", stderr="",
                          display="", error="ZeroDivisionError", elapsed=0.1, cell=3, added=[], removed=[],
                          namespace=[], images=[], interrupted=False)
    shown = sys.stderr.getvalue()
    assert "38;2;18;52;86" in shown and "38;5;" not in shown


def test_stderr_reasoning_keeps_its_heading_colour():
    from js.reasoning_display import StderrReasoning

    fset.set_entry("REASONING_HEADING", "%C04HEAD%C")
    stream = _Terminal()
    reasoning = StderrReasoning(2, stream)
    reasoning.append("thinking\n")
    assert f"{fset.fg('PALETTE_04')}HEAD" in stream.getvalue()


def test_a_network_line_draws_through_its_format_banner_and_all():
    from js import stream_transport

    lines: list[str] = []
    stream_transport.install_sink(stream_transport.NetSink(level=lambda: 3, emit=lines.append))
    try:
        fset.set_entry("CATALOG_UPDATING", "catalog custom")
        stream_transport.say(3, msgs.CATALOG_UPDATING.said())
        fset.set_entry("CATALOG_UPDATING", "%C04$BANNER catalog")
        stream_transport.say(3, msgs.CATALOG_UPDATING.said())
    finally:
        stream_transport.install_sink(None)
    assert [fset.plain(line) for line in lines] == ["catalog custom", f"{fset.banner_text()} catalog"]


def test_a_kernel_lifecycle_note_keeps_its_format_colour(monkeypatch, tmp_path):
    from js.toolkit import kernel as kmod

    monkeypatch.setattr(sys, "stderr", _Terminal())
    fset.set_entry("KERNEL_RESTARTED", "%C04RESTARTED%C")
    kmod.render_entry(type("Ctx", (), {"kernel_render_max_lines": 24})(), "normal",
                      msgs.KERNEL_RESTARTED.body_runs(), style="COLOR_WARN")
    assert "38;2;255;0;0mRESTARTED" in sys.stderr.getvalue()


def _closed(text: str) -> bool:
    """Every style the text opens is closed by a reset after it."""
    import re

    codes = list(re.finditer(r"\x1b\[([0-9;]*)m", text))
    last_open = max((m.end() for m in codes if m.group(1) not in ("0", "", "39")), default=-1)
    return last_open < 0 or any(m.start() >= last_open and m.group(1) in ("0", "") for m in codes)


def test_every_style_js_opens_on_a_speaker_line_is_closed(monkeypatch):
    from js import display

    for no_color in ("", "1"):
        monkeypatch.setenv("NO_COLOR", no_color)
        for mark_colour in ("#ff0000", ""):
            fset.reset()
            if mark_colour:
                fset.set_entry("COLOR_USER_MARK", mark_colour)
            else:
                fset.set_entry("COLOR_USER_MARK", "$NOT_A_COLOUR")
                fset.set_entry("COLOR_AGENT_MARK", "$NOT_A_COLOUR")
            for text in (display.user_line("hello"), display.prompt(), display.agent_speaker()[0],
                         msgs.SAVE_FAILED.line(error="x"), fset.render("%C04a%C b %s", ["c"])):
                assert _closed(text), (no_color, mark_colour, repr(text))


def test_save_puts_a_changed_line_after_a_load_that_would_override_it(tmp_path):
    child = tmp_path / "child.jsrc"
    child.write_text("fset THEME github\nset BANNER >>\n", encoding="utf-8")
    path = tmp_path / "jsrc"
    path.write_text(f"fset THEME crush\nset BANNER ***\nload {child}\n", encoding="utf-8")
    live = settings.seed_defaults()
    settings.set_dotted(live, ("BANNER",), "-!-")
    fset.set_entry("THEME", "paper")
    settings.save_settings_to_jsrc(path, live)
    fset.reset()
    loaded = settings.seed_defaults()
    settings.load_jsrc_files([path], loaded)
    assert fset.theme_name() == "paper" and settings.knob(loaded, "BANNER") == "-!-"


def test_setting_the_banner_confirms_under_the_new_banner_on_a_terminal(monkeypatch):
    monkeypatch.setattr(sys, "stdout", _Terminal())
    monkeypatch.setattr(msgs, "BANNER", "***")
    result = setcmd.set_command(settings.seed_defaults(), "BANNER -!-")
    assert fset.plain(result.lines[0]).startswith("-!- "), result.lines


def test_save_keeps_the_assignments_a_load_reads_and_moves_a_theme_below_it(tmp_path):
    child = tmp_path / "child.jsrc"
    child.write_text("alias kid set BANNER kid\nfset THEME github\n", encoding="utf-8")
    path = tmp_path / "jsrc"
    path.write_text(f"fset THEME crush\nset MOTD_FILE {child}\nload $MOTD_FILE\nset MOTD_FILE /nowhere\n",
                    encoding="utf-8")
    live = settings.seed_defaults()
    settings.load_jsrc_files([path], live)
    fset.set_entry("THEME", "crush")
    settings.save_settings_to_jsrc(path, live)
    fset.reset()
    loaded = settings.seed_defaults()
    settings.load_jsrc_files([path], loaded)
    assert fset.theme_name() == "crush"
    state = {"messages": [], "system": "s", "settings": loaded, "aliases": {}}
    cli._run_rc_commands(state, type("Cfg", (), {"settings": loaded, "project_dir": tmp_path})(), [path])
    assert "kid" in state["aliases"]


def test_every_fallback_printer_writes_plain_text_to_a_stream_that_is_not_a_terminal(monkeypatch, capsys):
    from js import display, runtime, stream_transport
    from js.reasoning_display import StderrReasoning

    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    fset.set_entry("RUN_LINE", "%C04run %s")
    stream_transport.say_or_print(3, msgs.CATALOG_UPDATING.said())
    runtime._print_run_line(["model=m", "ctx=1"])
    runtime._print_trace(runtime.Telemetry(None), display.render_exchange("shell", {"command": "x"}, None, level=1))
    msgs.say(msgs.SAVE_FAILED, error="disk full")
    msgs.warn(msgs.SAVE_FAILED, error="disk full")
    answer = display.Display.for_stream(out)
    answer.speaker(*display.agent_speaker())
    answer.chunk("text", "an answer\n")
    answer.finish()
    display.echo_user("a question")
    display.print_answer("**bold** answer", out)
    reasoning = StderrReasoning(2, err)
    reasoning.append("thinking\n")
    reasoning.finish(12)
    assert out.getvalue() and err.getvalue()
    assert "\x1b" not in out.getvalue() + err.getvalue(), (out.getvalue(), err.getvalue())


def test_an_fset_validation_refusal_words_its_reason_through_the_reason_s_format(capsys):
    fset.set_entry("FSET_NO_CODE_STYLE", "CUSTOM_BAD_STYLE %s")
    state = {"messages": [], "system": "s", "settings": settings.seed_defaults()}
    cli._handle_command("/fset CODE_STYLE no-such-style", state, type("Cfg", (), {"settings": None})())
    assert "CUSTOM_BAD_STYLE" in fset.plain(capsys.readouterr().out)


def test_a_raised_refusal_is_shown_through_its_own_format(capsys):
    from js.toolkit import policy

    fset.set_entry("POLICY_ENTRY_SHAPE", "CUSTOM_SHAPE %s %s")
    try:
        policy.parse_entry("noshape", "agent x")
    except policy.ToolPolicyError as exc:
        msgs.say(msgs.FAILED, error=exc)
    assert "CUSTOM_SHAPE" in capsys.readouterr().out


def test_a_message_inside_another_keeps_its_own_colours(monkeypatch):
    monkeypatch.setattr(sys, "stdout", _Terminal())
    fset.set_entry("EXPECTED_LEVEL", "%C04LEVEL_SENTINEL%C")
    result = setcmd.fset_command(settings.seed_defaults(), "TOOLS_DISPLAY 5")
    assert fset.fg("PALETTE_04") + "LEVEL_SENTINEL" in msgs.failure_line(result.error)


def test_cost_without_a_price_speaks_through_its_format(monkeypatch, capsys):
    from js import usage

    fset.set_entry("COST_UNPRICED", "CUSTOM_NO_PRICE")
    tally = usage.UsageTotals(calls=1, unpriced_calls=1)
    monkeypatch.setattr(usage, "totals", lambda _path: tally)
    cli._cmd_cost("", {}, type("Cfg", (), {"session_file": None})())
    assert "CUSTOM_NO_PRICE" in capsys.readouterr().out


def test_help_describes_each_command_through_its_format(capsys):
    for name in ("CMD_SET", "HELP_ALIAS", "HELP_ATTACH"):
        fset.set_entry(name, f"CUSTOM_{name} %s")
    state = {"messages": [], "system": "s", "settings": settings.seed_defaults(), "aliases": {"hi": "say hi"}}
    cli._cmd_help("", state, type("Cfg", (), {"settings": None})())
    shown = fset.plain(capsys.readouterr().out)
    assert all(f"CUSTOM_{name}" in shown for name in ("CMD_SET", "HELP_ALIAS", "HELP_ATTACH"))


def test_a_provider_error_speaks_through_its_format_where_it_is_shown():
    from js import routing

    fset.set_entry("PROVIDER_NOT_LOGGED_IN", "CUSTOM_LOGIN %s")
    fset.set_entry("PROVIDER_AUTH_FAILED", "CUSTOM_AUTH %s %s")
    for error in (routing.ProviderNotLoggedInError(routing.not_logged_in_message("acme")),
                  RuntimeError(msgs.PROVIDER_AUTH_FAILED.said(provider="acme", detail="no"))):
        shown = msgs.FAILED.line(colour=False, error=cli._error_text(error))
        assert "CUSTOM_" in shown, shown


def test_a_format_that_writes_its_own_banner_keeps_it_in_a_row():
    fset.set_entry("KERNEL_RESTARTED", "$BANNER RESTART_SENTINEL")
    shown = fset.to_plain(msgs.KERNEL_RESTARTED.body_runs())
    assert shown.startswith(fset.banner_text()) and "RESTART_SENTINEL" in shown
    fset.unset_entry("KERNEL_RESTARTED")
    assert not fset.to_plain(msgs.KERNEL_RESTARTED.body_runs()).startswith(fset.banner_text())
