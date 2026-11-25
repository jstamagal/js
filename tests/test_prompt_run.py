"""A `-p` run through `cli.main`: the exit code each ending gives, the
session_end handlers and the Continue line that follow every ending, what the
session holds, the stats file, and the JSON records of a run that saves
nothing or is interrupted."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import ai
import ai.types.usage
import pytest
from repl_driver import turn_stub
from test_agent_messages import _agent, _model
from turn_driver import js

from js import compaction, messages as msgs, runtime, session_store, snippets
from js.memory import load_messages
from js.model_client import ModelStreamResult

CONTINUE = msgs.CONTINUE_HINT.text(command="")


def _session(tmp_path, name: str):
    return session_store.folder_for(tmp_path) / f"{name}.jsonl"


def _records(out: str) -> list[dict]:
    return [json.loads(line) for line in out.splitlines()]


def _answering(text: str):
    def turn(cfg, system, messages, telemetry, **kwargs):
        messages.append({"role": "assistant", "content": text})
    return turn


def _raising(failure: BaseException):
    def turn(cfg, system, messages, telemetry, **kwargs):
        raise failure
    return turn


def test_an_empty_prompt_exits_2_before_any_request(tmp_path, monkeypatch, capsys):
    _agent(tmp_path, monkeypatch, {})
    requests = _model(monkeypatch, [])

    assert js(monkeypatch, "--session", "kept", "-p", "   ") == 2

    assert requests == []
    assert not _session(tmp_path, "kept").exists()
    assert CONTINUE not in capsys.readouterr().err


def test_an_attachment_that_is_not_there_exits_2_before_any_request(tmp_path, monkeypatch, capsys):
    _agent(tmp_path, monkeypatch, {})
    requests = _model(monkeypatch, [])

    assert js(monkeypatch, "--session", "kept", "-f", str(tmp_path / "missing.txt"), "-p", "look") == 2

    assert requests == []
    assert [m for m in load_messages(_session(tmp_path, "kept")) if m["role"] == "user"] == []
    assert CONTINUE in capsys.readouterr().err


@pytest.mark.parametrize("turn, code", [
    (_answering("done"), 0),
    (_answering(""), 1),
    (_raising(RuntimeError("provider down")), 1),
    (_raising(KeyboardInterrupt()), 130),
], ids=["answered", "no answer", "failed", "interrupted"])
def test_session_end_runs_then_the_continue_line_prints_however_the_run_ends(tmp_path, monkeypatch, capsys,
                                                                              turn, code):
    _agent(tmp_path, monkeypatch, {})
    (tmp_path / ".js" / "jsrc").write_text("on session_end flush\n", encoding="utf-8")
    monkeypatch.setattr(runtime, "run_turn_async", turn_stub(turn))

    assert js(monkeypatch, "--session", "kept", "-p", "look") == code

    err = capsys.readouterr().err
    ended = err.index(msgs.NO_QUEUED_PROMPTS.text())
    assert ended < err.index(CONTINUE)
    assert err.count(CONTINUE) == 1


@pytest.mark.parametrize("flag", ["-q", "-n"])
def test_a_quiet_or_unsaved_run_prints_no_continue_line(tmp_path, monkeypatch, capsys, flag):
    _agent(tmp_path, monkeypatch, {})
    _model(monkeypatch, ["done"])

    assert js(monkeypatch, flag, "-p", "look") == 0

    captured = capsys.readouterr()
    assert captured.out == "done\n"
    assert CONTINUE not in captured.err


def test_a_resumed_run_after_the_agent_prompt_changed_is_told_once(tmp_path, monkeypatch):
    directory = _agent(tmp_path, monkeypatch, {})
    requests = _model(monkeypatch, ["one", "two", "three"])
    session = _session(tmp_path, "kept")

    assert js(monkeypatch, "--session", "kept", "-p", "first") == 0
    (directory / "00-prompt.md").write_text("CHANGED", encoding="utf-8")
    assert js(monkeypatch, "--session", "kept", "-p", "second") == 0
    assert js(monkeypatch, "--session", "kept", "-p", "third") == 0

    notice = snippets.render("prompt-changed")
    users = [m["content"] for m in load_messages(session) if m["role"] == "user"]
    assert users == ["first", notice, "second", "third"]
    # The session keeps the system prompt it started with.
    assert requests[2][0] == requests[0][0]


def test_a_stats_file_records_the_answered_turn(tmp_path, monkeypatch):
    _agent(tmp_path, monkeypatch, {})
    _model(monkeypatch, ["done"])
    path = tmp_path / "stats.json"

    assert js(monkeypatch, "-n", "--stats-json", str(path), "-p", "question") == 0

    written = json.loads(path.read_text(encoding="utf-8"))
    assert (written["agent"], written["model"]) == ("voice", "offline-test-model")
    [row] = written["turns"]
    assert (row["prompt"], row["ok"], row["error"]) == ("question", True, None)


def test_a_json_run_that_saves_nothing_names_no_session(tmp_path, monkeypatch, capsys):
    _agent(tmp_path, monkeypatch, {})
    _model(monkeypatch, ["done"])

    assert js(monkeypatch, "--json-stream", "-n", "-p", "look") == 0

    records = _records(capsys.readouterr().out)
    assert (records[0]["type"], records[0]["id"], records[0]["file"]) == ("start", None, None)
    assert (records[-1]["type"], records[-1]["session"], records[-1]["answer"]) == ("end", None, "done")


def test_an_interrupted_json_run_ends_with_an_error_then_exit_130(tmp_path, monkeypatch, capsys):
    _agent(tmp_path, monkeypatch, {})
    monkeypatch.setattr(runtime, "run_turn_async", turn_stub(_raising(KeyboardInterrupt())))

    assert js(monkeypatch, "--json-stream", "--session", "kept", "-p", "look") == 130

    records = _records(capsys.readouterr().out)
    assert records[0]["type"] == "start"
    assert [record["type"] for record in records[-2:]] == ["error", "end"]
    assert (records[-1]["ok"], records[-1]["exit_code"]) == (False, 130)


def test_a_run_that_saves_nothing_does_not_compact(tmp_path, monkeypatch):
    _agent(tmp_path, monkeypatch, {})

    def stream(**kwargs):
        kwargs["on_text"]("done")
        return ModelStreamResult(
            text="done", tool_calls=[], reasoning="",
            usage=ai.types.usage.Usage(input_tokens=180_000, output_tokens=1),
            finish_reason="stop", assistant_message=ai.assistant_message("done"),
        )

    monkeypatch.setattr(runtime.model_client, "stream_model_async", stream)
    compact = AsyncMock(return_value=msgs.COMPACTED.said(keep_from=1, total=2, model="fixture"))
    monkeypatch.setattr(compaction, "compact_now", compact)
    window = ["--extra", 'CONTEXT_SIZE_PER_MODEL={"offline-test-model": 200000}']

    assert js(monkeypatch, *window, "-n", "-p", "look") == 0
    assert compact.await_count == 0
    assert js(monkeypatch, *window, "--session", "kept", "-p", "look") == 0
    assert compact.await_count == 1
