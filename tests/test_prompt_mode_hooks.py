"""`js -p` runs the `on` handlers of its jsrc files whose command stands alone,
on the events and in the order the REPL raises them; a handler whose command
needs the REPL is reported on stderr when it is registered and never runs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from turn_driver import js, script

from js import messages as msgs, runtime
from js.toolkit import ToolContext


@pytest.fixture
def project(tmp_path, monkeypatch) -> Path:
    """A project directory to run js in, its home elsewhere."""
    root = tmp_path / "project"
    (root / ".js").mkdir(parents=True)
    monkeypatch.chdir(root)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SHELL", "/bin/sh")
    monkeypatch.setattr(runtime.T, "STOCK_CONTEXT", ToolContext(cwd=root))
    return root


def _jsrc(project: Path, *lines: str) -> None:
    (project / ".js" / "jsrc").write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")


def test_a_turn_end_handler_runs(project, tmp_path, monkeypatch, capsys):
    marker = tmp_path / "turn-ended"
    _jsrc(project, f"on turn_end exec touch {marker}")
    script(tmp_path, monkeypatch, [])

    assert js(monkeypatch, "-n", "-p", "hi") == 0

    assert marker.exists()
    assert capsys.readouterr().out == "done\n"


def test_a_tool_call_handler_reads_the_call(project, tmp_path, monkeypatch):
    seen = tmp_path / "call.json"
    _jsrc(project, f"on tool_call exec cat > {seen}")
    script(tmp_path, monkeypatch, [[("c1", "read", '{"file_path": "notes.txt"}')]])

    assert js(monkeypatch, "-n", "-p", "hi") == 0

    call = json.loads(seen.read_text(encoding="utf-8"))
    assert (call["event"], call["id"], call["name"]) == ("tool_call", "c1", "read")


def test_a_tool_call_handler_refuses_the_call(project, tmp_path, monkeypatch):
    _jsrc(project, "on tool_call exec sh -c 'echo not here >&2; exit 2'")
    turn = script(tmp_path, monkeypatch, [[("c1", "read", '{"file_path": "notes.txt"}')]])

    assert js(monkeypatch, "-n", "-p", "hi") == 0

    [results] = turn.received_results()
    assert results["c1"].startswith("ERROR: not here")


def test_handlers_fire_on_the_events_the_repl_raises_in_its_order(project, tmp_path, monkeypatch):
    log = tmp_path / "events"
    names = ("session_end", "turn_end", "input", "turn_start", "session_start")
    _jsrc(project, *(f'on {name} exec echo "$JS_EVENT" >> {log}' for name in names))
    script(tmp_path, monkeypatch, [])

    assert js(monkeypatch, "-n", "-p", "hi") == 0

    assert log.read_text(encoding="utf-8").split() == [
        "session_start", "input", "turn_start", "turn_end", "session_end"]


def test_a_handler_that_needs_the_repl_is_reported_once_and_not_run(project, tmp_path, monkeypatch, capsys):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    # `prompt` fires before each model call: twice in this turn.
    _jsrc(project, f"on prompt cd {elsewhere}")
    script(tmp_path, monkeypatch, [[("c1", "read", '{"file_path": "notes.txt"}')]])

    assert js(monkeypatch, "-n", "-p", "hi") == 0

    captured = capsys.readouterr()
    assert Path.cwd() == project
    assert captured.out == "done\n"
    assert captured.err.count(msgs.ON_HANDLER_NEEDS_REPL.text(event="prompt", command="cd")) == 1


@pytest.mark.parametrize("flags", [(), ("--json",), ("--json-stream",)])
def test_a_broken_handler_says_so_on_stderr_and_the_run_goes_on(project, tmp_path, monkeypatch, capsys, flags):
    _jsrc(project, "on turn_end exec sh -c 'echo partial; echo broken >&2; exit 3'",
          "on session_end flush")
    script(tmp_path, monkeypatch, [])

    assert js(monkeypatch, *flags, "-n", "-p", "hi") == 0

    captured = capsys.readouterr()
    # What a handler's command prints is not the answer.
    said = msgs.NO_QUEUED_PROMPTS.text()
    assert "broken" in captured.err and said in captured.err
    assert said not in captured.out
    if not flags:
        assert captured.out == "done\n"
    else:
        records = [json.loads(line) for line in captured.out.splitlines()]
        assert records[-1]["answer"] == "done"
