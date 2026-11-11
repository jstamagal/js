"""Terminal protocol handling preserves output across control sequences."""

import json
import re
import shlex
import sys

import pytest

from js.toolkit.core import ToolContext
from js.toolkit.terminal import close_terminal_sessions, terminal_session


def _run_output(tmp_path, output):
    context = ToolContext(cwd=tmp_path)
    script = f"import sys; sys.stdout.write({output!r}); sys.stdout.flush()"
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
    try:
        return json.loads(terminal_session("start", command=command, wait_ms=300, context=context))
    finally:
        close_terminal_sessions(context)


@pytest.mark.parametrize("sequence", ["\x1b[?4m", "\x1b[?6n", "\x1b[?1A", "\x1b[?9999z"])
def test_private_csi_preserves_following_output(tmp_path, sequence):
    result = _run_output(tmp_path, sequence + "AFTER")
    assert any("AFTER" in line for line in result["lines"])


def test_fragmented_private_csi():
    import pyte
    from js.toolkit.terminal import _make_screen

    screen = _make_screen(80, 24)
    stream = pyte.Stream(screen)
    for chunk in ("\x1b[", "?", "4", "mAFTER", "\x1b[?1h"):
        stream.feed(chunk)
    assert screen.display[0].strip() == "AFTER"
    assert 1 << 5 in screen.mode


@pytest.mark.parametrize("key,final", [("up", "A"), ("down", "B"), ("right", "C"), ("left", "D"), ("home", "H"), ("end", "F")])
@pytest.mark.parametrize("application", [False, True])
def test_cursor_keys_follow_drained_mode(tmp_path, monkeypatch, key, final, application):
    import pyte
    from js import jail
    from js.toolkit import terminal

    context = ToolContext(cwd=tmp_path)
    screen = terminal._make_screen(80, 24)
    stream = pyte.Stream(screen)
    # Begin in the opposite mode; pending output decides the mode at send time.
    stream.feed("\x1b[?1l" if application else "\x1b[?1h")
    sent = []

    class Child:
        def isalive(self):
            return True

        def send(self, value):
            sent.append(value)

    context.terminal_sessions["main"] = {"child": Child(), "screen": screen, "jail": jail.shape(context)}
    monkeypatch.setattr(terminal, "_drain", lambda *_: stream.feed("\x1b[?1h" if application else "\x1b[?1l"))
    monkeypatch.setattr(terminal, "_observe", lambda *_: "observed")
    monkeypatch.setattr(terminal.time, "sleep", lambda _: None)
    assert terminal_session("send", keys=key, context=context) == "observed"
    assert sent == [("\x1bO" if application else "\x1b[").encode() + final.encode()]


@pytest.mark.parametrize("query,expected", [
    ("\x1b[c", "\x1b[?6c"),
    ("\x1b[>c", "\x1b[>0;0;0c"),
    ("\x1b[6n", "\x1b[1;1R"),
    ("\x1b[5n", "\x1b[0n"),
])
def test_queries_reply_to_actual_pty(tmp_path, query, expected):
    context = ToolContext(cwd=tmp_path)
    script = (
        "import os,select,tty; tty.setraw(0); "
        f"os.write(1, {query.encode()!r}); "
        "ready=select.select([0],[],[],1)[0]; "
        "reply=os.read(0,1024) if ready else b''; "
        "os.write(1,b'REPLY:'+reply.hex().encode())"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
    try:
        result = json.loads(terminal_session("start", command=command, wait_ms=1500, context=context))
        assert any("REPLY:" + expected.encode().hex() in line for line in result["lines"])
    finally:
        close_terminal_sessions(context)


def test_fragmented_secondary_query_and_adjacent_output():
    from js.toolkit.terminal import _make_screen, _TerminalStream

    replies = []
    screen = _make_screen(80, 24, replies.append)
    stream = _TerminalStream(screen)
    for chunk in ("BEFORE\x1b", "[", ">", "0", "cAFTER\x1b[31m RED"):
        stream.feed(chunk)
    assert replies == [b"\x1b[>0;0;0c"]
    assert screen.display[0].strip() == "BEFOREAFTER RED"
    assert screen.buffer[0][12].fg == "red"


@pytest.mark.parametrize("key,expected", [
    *[(f"ctrl-{chr(letter)}", bytes([letter - 96])) for letter in range(97, 123)],
    ("CTRL-R", b"\x12"),
    ("alt-x", b"\x1bx"),
    ("alt-X", b"\x1bX"),
    ("alt-enter", b"\x1b\r"),
    ("alt-left", b"\x1b\x1b[D"),
    ("shift-tab", b"\x1b[Z"),
])
def test_key_chords_reach_actual_pty(tmp_path, key, expected):
    context = ToolContext(cwd=tmp_path)
    script = (
        "import os,tty; tty.setraw(0); os.write(1,b'READY'); "
        f"data=b''\nwhile len(data)<{len(expected)}: data+=os.read(0,1024)\n"
        "os.write(1,b'RECEIVED:'+data.hex().encode())"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
    try:
        started = json.loads(terminal_session("start", command=command, wait_ms=200, context=context))
        assert any("READY" in line for line in started["lines"])
        result = json.loads(terminal_session("send", keys=key, wait_ms=200, context=context))
        received = re.search(r"RECEIVED:([0-9a-f]+)", "".join(result["lines"]))
        assert received is not None
        assert bytes.fromhex(received.group(1)) == expected
    finally:
        close_terminal_sessions(context)


def test_literal_text_precedes_keys_without_interpreting_commas(tmp_path):
    context = ToolContext(cwd=tmp_path)
    text = "echo a, b,enter,ctrl-r,comma"
    expected = text.encode() + b"\r"
    script = (
        "import os,tty; tty.setraw(0); os.write(1,b'READY'); "
        f"data=b''\nwhile len(data)<{len(expected)}: data+=os.read(0,1024)\n"
        "os.write(1,b'RECEIVED:'+data.hex().encode())"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
    try:
        started = json.loads(terminal_session("start", command=command, wait_ms=200, context=context))
        assert any("READY" in line for line in started["lines"])
        result = json.loads(terminal_session("send", text=text, keys="enter", wait_ms=200, context=context))
        received = re.search(r"RECEIVED:([0-9a-f]+)", "".join(result["lines"]))
        assert received is not None
        assert bytes.fromhex(received.group(1)) == expected
    finally:
        close_terminal_sessions(context)


def test_text_is_exposed_in_terminal_tool_schema():
    from js.toolkit.terminal import tools

    tool = next(tool for tool in tools() if tool.name == "terminal_session")
    assert tool.params["text"] == {"type": "string"}


@pytest.mark.parametrize("token,expected", [
    ("alt-left", "\x1b\x1bOD"),
    ("alt-ctrl-r", "\x1b\x12"),
    ("comma", ","),
    ("literal text", "literal text"),
    ("ctrl-1", "ctrl-1"),
])
def test_key_mapping_preserves_legacy_text_and_application_mode(token, expected):
    from js.toolkit.terminal import _key_sequence

    assert _key_sequence(token, application_cursor=True) == expected


def test_standard_sgr_still_renders(tmp_path):
    result = _run_output(tmp_path, "\x1b[31mRED\x1b[0m AFTER")
    assert result["lines"][0] == "RED AFTER"
