"""Patch coverage ends on the last matched line, not after its newline."""

import pytest

from js.toolkit import fs
from js.toolkit.core import ToolContext


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("matched_lines", [1, 2])
def test_line_span_excludes_line_after_terminal_newline(newline, matched_lines):
    text = newline.join(["one", "", "three", "four", ""])
    end = len(newline.join(["one", ""] + ([""] if matched_lines == 2 else [])))

    assert fs._line_span(text, 0, end) == (1, matched_lines)
    assert fs._line_span(text, 0, end + 1) == (1, matched_lines + 1)


@pytest.mark.parametrize("newline", [b"\n", b"\r\n", b"\r"])
def test_patch_line_including_newline_needs_only_that_line_read(tmp_path, newline):
    target = tmp_path / "file.txt"
    target.write_bytes(newline.join([b"one", b"two", b"three", b"four", b"five", b""]))
    context = ToolContext(cwd=tmp_path)
    fs.read(str(target), start_line=1, end_line=1, context=context)

    result = fs.patch(str(target), old_string="one\n", new_string="A\nB\n", context=context)

    assert result.startswith("patched ")
    assert target.read_bytes() == newline.join([b"A", b"B", b"two", b"three", b"four", b"five", b""])
    assert context.read_ranges[target] == [(1, 2)]
    # Inserted text is known, but the formerly second line remains unread.
    assert fs.patch(str(target), old_string="B", new_string="BB", context=context).startswith("patched ")
    assert fs.patch(str(target), old_string="two", new_string="TWO", context=context).startswith("ERROR:")


@pytest.mark.parametrize("newline", [b"\n", b"\r\n", b"\r"])
def test_patch_that_reaches_following_unread_line_is_still_refused(tmp_path, newline):
    target = tmp_path / "file.txt"
    original = newline.join([b"one", b"two", b"three", b""])
    target.write_bytes(original)
    context = ToolContext(cwd=tmp_path)
    fs.read(str(target), start_line=1, end_line=1, context=context)

    result = fs.patch(str(target), old_string="one\nt", new_string="changed", context=context)

    assert result.startswith("ERROR:")
    assert target.read_bytes() == original
    assert not context.snapshots
