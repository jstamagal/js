"""Leaving a session and coming back: the exit hint, --last, the two-^C guard,
and /quit with a closing note."""

from __future__ import annotations

import json
import shlex

from js import cli
from js.config import from_env
from js.memory import load_messages
from repl_driver import LineSession, run_blocking


def _repl(monkeypatch, tmp_path, argv, lines=()):
    """Launch `js --blocking *argv` over *lines*; it exits on EOF."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_AGENT", raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)
    monkeypatch.delenv("JS_MODEL", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli, "PromptSession", lambda *a, **k: LineSession(lines))
    monkeypatch.setattr(cli.runtime, "run_turn", lambda *a, **k: None)
    return cli.main(["--blocking", *argv])


def _session(monkeypatch, tmp_path, lines, **state_kwargs):
    """Run the blocking loop over *lines* on a fresh session; return its state and config."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.runtime, "run_turn", lambda *a, **k: None)
    cfg = from_env()
    return run_blocking(cfg, lines, **state_kwargs), cfg


def _only_session(tmp_path):
    found = list((tmp_path / ".js" / "sessions").rglob("*.jsonl"))
    assert len(found) == 1, found
    return found[0]


def _resume_args(output: str) -> list[str]:
    last = output.rstrip("\n").splitlines()[-1]
    return shlex.split(last[last.index("js "):])


def test_exit_prints_a_runnable_resume_command(monkeypatch, tmp_path, capsys):
    state, cfg = _session(monkeypatch, tmp_path, ["hello"], model="cliapiproxy/claude-opus-5")
    capsys.readouterr()

    cli._print_resume_hint(cfg, state)

    args = _resume_args(capsys.readouterr().out)
    assert args == ["js", "--model", "cliapiproxy/claude-opus-5", "--session", cfg.session_file.stem]


def test_no_resume_hint_for_a_session_with_nothing_in_it(monkeypatch, tmp_path, capsys):
    state, cfg = _session(monkeypatch, tmp_path, [])
    capsys.readouterr()

    cli._print_resume_hint(cfg, state)

    assert capsys.readouterr().out == ""


class _InterruptedSession(LineSession):
    """The first *interrupts* prompts raise KeyboardInterrupt, standing in for ^C
    at an idle prompt; then *lines*, then EOF."""

    def __init__(self, lines, interrupts):
        super().__init__(lines)
        self.interrupts = interrupts

    def prompt(self, *args, **kwargs):
        if self.interrupts > 0:
            self.interrupts -= 1
            raise KeyboardInterrupt
        return super().prompt(*args, **kwargs)


def _interrupted(monkeypatch, tmp_path, lines, interrupts):
    state, _cfg = _session(monkeypatch, tmp_path, _InterruptedSession(lines, interrupts))
    return state


def test_a_second_interrupt_at_the_prompt_exits(monkeypatch, tmp_path):
    # Two ^C in a row end the loop before the line after them is read.
    state = _interrupted(monkeypatch, tmp_path, ["never read"], interrupts=2)

    assert state["messages"] == []


def test_an_interrupt_does_not_end_a_session_that_keeps_going(monkeypatch, tmp_path):
    # One ^C, then a real line: the session must survive to record it.
    _interrupted(monkeypatch, tmp_path, ["still here"], interrupts=1)

    roles = [m["role"] for m in load_messages(_only_session(tmp_path))]
    assert roles == ["user"]


def test_last_resumes_the_previous_session(monkeypatch, tmp_path):
    _repl(monkeypatch, tmp_path, [], lines=["first run"])
    session_file = _only_session(tmp_path)
    resumed: list = []

    def record(cfg, system, messages, *a, **k):
        resumed.append([m["content"] for m in messages])

    monkeypatch.setattr(cli, "PromptSession", lambda *a, **k: LineSession(["second run"]))
    monkeypatch.setattr(cli.runtime, "run_turn", record)
    assert cli.main(["--blocking", "--last"]) == 0

    assert _only_session(tmp_path) == session_file
    assert resumed == [["first run", "second run"]]


def test_last_reports_when_there_is_nothing_to_resume(monkeypatch, tmp_path):
    assert _repl(monkeypatch, tmp_path, ["--last"]) == 2


def test_last_refuses_to_fight_an_explicit_session(monkeypatch, tmp_path):
    assert _repl(monkeypatch, tmp_path, ["--last", "--session", "somewhere"]) == 2


def test_quit_with_a_note_records_it_for_the_next_turn(monkeypatch, tmp_path):
    _session(monkeypatch, tmp_path, ["/quit back in an hour"])

    messages = load_messages(_only_session(tmp_path))
    assert [m["role"] for m in messages] == ["user"]
    assert "back in an hour" in messages[0]["content"]
    assert "js-reminder" in messages[0]["content"]


def test_a_closing_note_never_leaves_two_user_messages_in_a_row(monkeypatch, tmp_path):
    # Strict-alternation providers reject back-to-back user turns outright.
    _session(monkeypatch, tmp_path, ["a question", "/quit heading out"])

    messages = load_messages(_only_session(tmp_path))
    roles = [m["role"] for m in messages]
    assert not any(a == b == "user" for a, b in zip(roles, roles[1:]))
    assert "heading out" in messages[-1]["content"]


def test_bare_quit_leaves_no_note(monkeypatch, tmp_path):
    _session(monkeypatch, tmp_path, ["a question", "/quit"])

    messages = load_messages(_only_session(tmp_path))
    assert "js-reminder" not in json.dumps(messages)
