"""A resume comes back on the model and agent the session was using, and says so
when the session it was pointed at turns out to be empty."""

from __future__ import annotations

import json

import pytest

from js import cli
from js.session_catalog import last_session_model, record_session_start


def _metadata(session_file):
    out = []
    for line in session_file.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get("kind") == "session_metadata":
            out.append(record)
    return out


def test_session_start_records_agent_and_model(tmp_path):
    session_file = tmp_path / "session.jsonl"

    record_session_start(
        session_file, cwd=tmp_path, agent="defaultagent", model="cliapiproxy/claude-opus-5"
    )

    assert _metadata(session_file)[0]["agent"] == "defaultagent"
    assert last_session_model(session_file) == "cliapiproxy/claude-opus-5"


def test_last_session_model_is_the_most_recent_one(tmp_path):
    session_file = tmp_path / "session.jsonl"

    record_session_start(session_file, cwd=tmp_path, model="first/model")
    record_session_start(session_file, cwd=tmp_path, model="second/model")

    assert last_session_model(session_file) == "second/model"


@pytest.mark.parametrize("model", [None, ""])
def test_last_session_model_is_none_when_never_recorded(tmp_path, model):
    session_file = tmp_path / "session.jsonl"

    record_session_start(session_file, cwd=tmp_path, model=model)

    assert last_session_model(session_file) is None


def test_last_session_model_survives_a_session_with_no_metadata(tmp_path):
    session_file = tmp_path / "session.jsonl"
    session_file.write_text(
        json.dumps({"kind": "message", "message": {"role": "user", "content": "hi"}}) + "\n",
        encoding="utf-8",
    )

    assert last_session_model(session_file) is None


def _repl(monkeypatch, tmp_path, argv, lines=()):
    """Run the interactive loop over *lines*, then exit."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_AGENT", raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)
    monkeypatch.delenv("JS_MODEL", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)

    class PromptSessionStub:
        def __init__(self, history, **kwargs):
            self.lines = iter(lines)

        def prompt(self, *_args, **_kwargs):
            return next(self.lines)

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


def test_resume_without_a_model_flag_reuses_the_recorded_model(monkeypatch, tmp_path, capsys):
    _repl(monkeypatch, tmp_path, ["--model", "cliapiproxy/claude-opus-5"])
    session_file = _only_session(tmp_path)
    capsys.readouterr()

    name = session_file.stem
    _repl(monkeypatch, tmp_path, ["--session", name])

    assert "cliapiproxy/claude-opus-5" in capsys.readouterr().out


def test_an_explicit_model_flag_still_wins_over_the_recorded_one(monkeypatch, tmp_path, capsys):
    _repl(monkeypatch, tmp_path, ["--model", "cliapiproxy/claude-opus-5"])
    session_file = _only_session(tmp_path)
    capsys.readouterr()

    _repl(monkeypatch, tmp_path, ["--session", session_file.stem, "--model", "other/model"])
    out = capsys.readouterr().out

    assert "cliapiproxy/claude-opus-5" not in out


def test_resuming_an_empty_session_says_so(monkeypatch, tmp_path, capsys):
    sessions = tmp_path / ".local" / "share" / "js" / "sessions" / "defaultagent"
    sessions.mkdir(parents=True)
    (sessions / "blank.jsonl").write_text("", encoding="utf-8")

    _repl(monkeypatch, tmp_path, ["--session", "blank"])

    assert "nothing to resume" in capsys.readouterr().out
