"""A resume comes back on the model and agent the session was using, and says so
when the session it was pointed at turns out to be empty."""

from __future__ import annotations

import json

import pytest

from js import cli
from js.session_catalog import last_session_model, record_session_start
from repl_driver import LineSession


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
    """Launch `js --blocking *argv` over *lines*; return the model each turn ran on."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_AGENT", raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)
    monkeypatch.delenv("JS_MODEL", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    models: list[str] = []
    monkeypatch.setattr(cli, "PromptSession", lambda *a, **k: LineSession(lines))
    monkeypatch.setattr(cli.runtime, "run_turn", lambda cfg, *a, **k: models.append(cfg.model))
    assert cli.main(["--blocking", *argv]) == 0
    return models


def _only_session(tmp_path):
    found = list((tmp_path / ".js" / "sessions").rglob("*.jsonl"))
    assert len(found) == 1, found
    return found[0]


def test_resume_without_a_model_flag_reuses_the_recorded_model(monkeypatch, tmp_path):
    _repl(monkeypatch, tmp_path, ["--model", "cliapiproxy/claude-opus-5"])
    session_file = _only_session(tmp_path)

    models = _repl(monkeypatch, tmp_path, ["--session", session_file.stem], lines=["go on"])

    assert models == ["cliapiproxy/claude-opus-5"]


def test_an_explicit_model_flag_still_wins_over_the_recorded_one(monkeypatch, tmp_path):
    _repl(monkeypatch, tmp_path, ["--model", "cliapiproxy/claude-opus-5"])
    session_file = _only_session(tmp_path)

    models = _repl(monkeypatch, tmp_path, ["--session", session_file.stem, "--model", "other/model"], lines=["go on"])

    assert models == ["other/model"]
