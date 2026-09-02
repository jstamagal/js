"""Leaving a session and coming back: the exit hint, --last, the two-^C guard,
and /quit with a closing note."""

from __future__ import annotations

import json


from js import cli
from js.memory import load_messages


def _repl(monkeypatch, tmp_path, argv, lines=(), interrupts=0):
    """Run the interactive loop over *lines*.

    The first *interrupts* prompts raise KeyboardInterrupt, standing in for ^C at
    an idle prompt; the loop then reads *lines* and exits on EOF.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_AGENT", raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)
    monkeypatch.delenv("JS_MODEL", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)

    state = {"interrupts": interrupts}

    class PromptSessionStub:
        def __init__(self, history, **kwargs):
            self.lines = iter(lines)

        def prompt(self, *_args, **_kwargs):
            if state["interrupts"] > 0:
                state["interrupts"] -= 1
                raise KeyboardInterrupt
            try:
                return next(self.lines)
            except StopIteration:
                raise EOFError from None  # exit the way Ctrl-D does

    monkeypatch.setattr(cli, "PromptSession", PromptSessionStub)
    monkeypatch.setattr(cli.runtime, "run_turn", lambda *a, **k: None)
    try:
        return cli.main(argv)
    except (RuntimeError, StopIteration):
        return 0


def _only_session(tmp_path):
    found = list((tmp_path / ".local" / "share" / "js" / "sessions").rglob("*.jsonl"))
    assert len(found) == 1, found
    return found[0]


def test_exit_prints_a_runnable_resume_command(monkeypatch, tmp_path, capsys):
    _repl(monkeypatch, tmp_path, ["--model", "cliapiproxy/claude-opus-5"], lines=["hello"])
    out = capsys.readouterr().out
    session_file = _only_session(tmp_path)

    assert "Resume: " in out
    assert "js --model cliapiproxy/claude-opus-5" in out
    assert f"--session {session_file.stem}" in out


def test_no_resume_hint_for_a_session_with_nothing_in_it(monkeypatch, tmp_path, capsys):
    _repl(monkeypatch, tmp_path, [])

    assert "Resume:" not in capsys.readouterr().out


def test_one_interrupt_warns_and_the_second_exits(monkeypatch, tmp_path, capsys):
    assert _repl(monkeypatch, tmp_path, [], interrupts=2) == 0

    assert "press ^C again to exit" in capsys.readouterr().out


def test_an_interrupt_does_not_end_a_session_that_keeps_going(monkeypatch, tmp_path, capsys):
    # One ^C, then a real line: the session must survive to record it.
    _repl(monkeypatch, tmp_path, [], lines=["still here"], interrupts=1)

    roles = [m["role"] for m in load_messages(_only_session(tmp_path))]
    assert roles == ["user"]


def test_last_resumes_the_previous_session(monkeypatch, tmp_path, capsys):
    _repl(monkeypatch, tmp_path, [], lines=["first run"])
    session_file = _only_session(tmp_path)
    capsys.readouterr()

    _repl(monkeypatch, tmp_path, ["--last"])

    assert "resumed: 1 prior messages" in capsys.readouterr().out
    assert _only_session(tmp_path) == session_file


def test_last_reports_when_there_is_nothing_to_resume(monkeypatch, tmp_path, capsys):
    assert _repl(monkeypatch, tmp_path, ["--last"]) == 2

    assert "no previous session" in capsys.readouterr().err


def test_last_refuses_to_fight_an_explicit_session(monkeypatch, tmp_path, capsys):
    assert _repl(monkeypatch, tmp_path, ["--last", "--session", "somewhere"]) == 2

    assert "cannot be combined" in capsys.readouterr().err


def test_quit_with_a_note_records_it_for_the_next_turn(monkeypatch, tmp_path):
    _repl(monkeypatch, tmp_path, [], lines=["/quit back in an hour"])

    messages = load_messages(_only_session(tmp_path))
    assert [m["role"] for m in messages] == ["user"]
    assert "back in an hour" in messages[0]["content"]
    assert "js-reminder" in messages[0]["content"]


def test_a_closing_note_never_leaves_two_user_messages_in_a_row(monkeypatch, tmp_path):
    # Strict-alternation providers reject back-to-back user turns outright.
    _repl(monkeypatch, tmp_path, [], lines=["a question", "/quit heading out"])

    messages = load_messages(_only_session(tmp_path))
    roles = [m["role"] for m in messages]
    assert not any(a == b == "user" for a, b in zip(roles, roles[1:]))
    assert "heading out" in messages[-1]["content"]


def test_bare_quit_leaves_no_note(monkeypatch, tmp_path):
    _repl(monkeypatch, tmp_path, [], lines=["a question", "/quit"])

    messages = load_messages(_only_session(tmp_path))
    assert "js-reminder" not in json.dumps(messages)
