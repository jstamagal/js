"""Persistent terminals consume output even between tool calls."""

import json
import shlex
import sys
import time

import pytest

from js.toolkit import terminal
from js.toolkit.core import ToolContext


def _command(script):
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"


def test_eof_finalizes_split_utf8_and_keeps_final_screen(tmp_path):
    context = ToolContext(cwd=tmp_path)
    command = _command("import os,time; os.write(1,b'OK\\xe2'); time.sleep(.05); os.write(1,b'\\x82')")
    try:
        result = json.loads(terminal.terminal_session("start", command=command, wait_ms=500, context=context))
        assert any("OK\ufffd" in line for line in result["lines"])
        assert not result["still_running"]
        assert context.terminal_sessions["main"]["reader_done"]
    finally:
        terminal.close_terminal_sessions(context)


def test_flood_finishes_between_tool_calls(tmp_path):
    context = ToolContext(cwd=tmp_path)
    marker = tmp_path / "finished"
    command = _command(
        "import os,pathlib,time; time.sleep(.1); "
        "os.write(1,b'line of terminal output\\n'*20000); "
        f"pathlib.Path({str(marker)!r}).write_text('done')"
    )
    try:
        terminal.terminal_session("start", command=command, wait_ms=0, context=context)
        # No terminal observation is in flight during this interval.
        time.sleep(.8)
        assert marker.read_text() == "done"
        result = json.loads(terminal.terminal_session("look", wait_ms=0, context=context))
        assert any("line of terminal output" in line for line in result["lines"])
        reader = context.terminal_sessions["main"]["reader"]
        reader.join(timeout=1)
        assert not reader.is_alive()
    finally:
        terminal.close_terminal_sessions(context)


@pytest.mark.parametrize("action", ["stop", "replace", "close", "jail_refusal", "atexit"])
def test_reader_stops_with_its_session(tmp_path, action):
    context = ToolContext(cwd=tmp_path)
    command = _command("import time; time.sleep(60)")
    try:
        terminal.terminal_session("start", command=command, wait_ms=0, context=context)
        state = context.terminal_sessions["main"]
        reader = state["reader"]
        assert reader.is_alive()
        if action == "stop":
            terminal.terminal_session("stop", context=context)
        elif action == "replace":
            terminal.terminal_session("start", command=_command("print('replacement')"), wait_ms=0, context=context)
        elif action == "close":
            terminal.close_terminal_sessions(context)
        elif action == "atexit":
            terminal._cleanup_live_children()
        else:
            state["jail"] = ("different",)
            assert terminal.terminal_session("look", wait_ms=0, context=context).startswith("ERROR:")
        assert not reader.is_alive()
        assert not state["child"].isalive()
    finally:
        terminal.close_terminal_sessions(context)
