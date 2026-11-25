"""Leaving a session and coming back: the exit hint, --last, and /quit with a
closing note."""

from __future__ import annotations

import json
import shlex

from repl_driver import run_async, stub_screen, turn_stub

from js import cli
from js.config import from_env
from js.memory import load_messages


def _reply(cfg, system, messages, *a, **k):
    messages.append({"role": "assistant", "content": "ok"})


def _repl(monkeypatch, tmp_path, argv, lines=(), run_turn=lambda *a, **k: None):
    """Launch `js *argv` over *lines*; it exits on EOF."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_DEFAULT_AGENT", raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)
    monkeypatch.delenv("JS_MODEL", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    stub_screen(monkeypatch, lines, wait=True)
    monkeypatch.setattr(cli.runtime, "run_turn_async", turn_stub(run_turn))
    return cli.main([*argv])


def _session(monkeypatch, tmp_path, lines, **state_kwargs):
    """Run the REPL over *lines* on a fresh session; return its state and config."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.runtime, "run_turn_async", turn_stub(lambda *a, **k: None))
    cfg = from_env()
    return run_async(monkeypatch, cfg, lines, wait=True, **state_kwargs), cfg


def _only_session(tmp_path):
    found = list((tmp_path / ".js" / "sessions").rglob("*.jsonl"))
    assert len(found) == 1, found
    return found[0]


def _resume_args(output: str) -> list[str]:
    last = output.rstrip("\n").splitlines()[-1]
    return shlex.split(last[last.index("js "):])


def test_exit_prints_a_runnable_resume_command(monkeypatch, tmp_path, capsys):
    assert _repl(monkeypatch, tmp_path, ["--model", "cliapiproxy/claude-opus-5"], ["hello"], run_turn=_reply) == 0

    args = _resume_args(capsys.readouterr().out)
    assert args == ["js", "--model", "cliapiproxy/claude-opus-5", "--session", _only_session(tmp_path).stem]


def test_leaving_the_repl_prints_the_resume_command(monkeypatch, tmp_path, capsys):
    assert _repl(monkeypatch, tmp_path, ["--session", "kept"], ["hello"], run_turn=_reply) == 0

    assert _resume_args(capsys.readouterr().out)[-2:] == ["--session", "kept"]


def test_no_resume_hint_for_a_session_with_nothing_in_it(monkeypatch, tmp_path, capsys):
    assert _repl(monkeypatch, tmp_path, []) == 0

    assert "js --" not in capsys.readouterr().out


def test_last_resumes_the_previous_session(monkeypatch, tmp_path):
    _repl(monkeypatch, tmp_path, [], lines=["first run"], run_turn=_reply)
    session_file = _only_session(tmp_path)
    resumed: list = []

    def record(cfg, system, messages, *a, **k):
        resumed.append([m["content"] for m in messages])

    stub_screen(monkeypatch, ["second run"], wait=True)
    monkeypatch.setattr(cli.runtime, "run_turn_async", turn_stub(record))
    assert cli.main(["--last"]) == 0

    assert _only_session(tmp_path) == session_file
    assert resumed == [["first run", "ok", "second run"]]


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
