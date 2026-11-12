"""Shell plain output uses the shared terminal-sequence parser, not CSI alone."""

import shlex
import sys

import pytest

from js.toolkit import process_net
from js.toolkit.core import ToolContext


@pytest.mark.parametrize("text, expected", [
    ("\x1b]0;TITLE\x07A", "A"),
    ("\x1b]0;TITLE\x1b\\A", "A"),
    ("\x1b]8;;https://example.test\x07link\x1b]8;;\x07", "link"),
    ("\x1b]8;;https://example.test\x1b\\link\x1b]8;;\x1b\\", "link"),
    ("\x1b(0q\x1b(B", "q"),
    ("A\x1b7B\x1b8C", "ABC"),
    ("\x1b[38:2:1:2:3mRED\x1b[0m", "RED"),
    ("\x1bP1;2|hidden\x1b\\visible", "visible"),
    ("\x1b_hidden\x1b\\visible", "visible"),
    ("\x9d0;TITLE\x9c界", "界"),
    ("界\tline\rnext\n\x07", "界\tline\rnext\n\x07"),
    ("visible\x1b[31", "visible"),
    ("visible\x1b]unfinished title", "visibleunfinished title"),
])
def test_plain_shell_cleaning_and_exact_keep_ansi(text, expected):
    raw = text.encode("utf-8")

    assert process_net._clean(raw, keep_ansi=False) == expected
    assert process_net._clean(raw, keep_ansi=True) == text


@pytest.mark.parametrize("keep_ansi", [False, True])
def test_actual_shell_output_strips_osc_and_charset_without_losing_text(tmp_path, keep_ansi):
    raw = b"\x1b]0;TITLE\x07A\x1b]8;;https://example.test\x1b\\link\x1b]8;;\x1b\\B\x1b(0q\x1b(B\n"
    code = "import os; os.write(1, " + repr(raw) + ")"
    command = shlex.quote(sys.executable) + " -c " + shlex.quote(code)

    result = process_net.shell(command=command, keep_ansi=keep_ansi, context=ToolContext(cwd=tmp_path))

    if keep_ansi:
        assert raw.decode("utf-8") in result
    else:
        assert "AlinkBq\n" in result
        assert "\x1b" not in result
        assert "\x07" not in result
        assert "TITLE" not in result
