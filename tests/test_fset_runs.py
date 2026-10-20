"""One render step and its targets: a format becomes colour runs, and each
output target (ANSI, prompt_toolkit, rich, plain) is drawn from them, with
NO_COLOR applied by the targets."""

from __future__ import annotations

import io
import sys

from js import cli, fset, settings
from js import messages as msgs


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


def test_collecting_the_config_twice_does_not_compound_fset_entries(tmp_path):
    path = tmp_path / "jsrc"
    path.write_text("fset MARK_AGENT $MARK_AGENT>\n", encoding="utf-8")
    settings.collect_settings(config_paths=[path], env={})
    settings.collect_settings(config_paths=[path], env={})
    assert fset.lookup("MARK_AGENT") == "●>"


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
