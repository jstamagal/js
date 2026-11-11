"""Terminal protocol handling preserves output across control sequences."""

import json
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


def test_standard_sgr_still_renders(tmp_path):
    result = _run_output(tmp_path, "\x1b[31mRED\x1b[0m AFTER")
    assert result["lines"][0] == "RED AFTER"
