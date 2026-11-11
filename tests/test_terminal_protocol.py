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


def test_standard_sgr_still_renders(tmp_path):
    result = _run_output(tmp_path, "\x1b[31mRED\x1b[0m AFTER")
    assert result["lines"][0] == "RED AFTER"
