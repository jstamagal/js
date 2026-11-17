"""Patch coverage ends on the last matched line, not after its newline."""

import pytest

from js.toolkit import fs, patch
from js.toolkit.core import ToolContext


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("matched_lines", [1, 2])
def test_line_span_excludes_line_after_terminal_newline(tmp_path, newline, matched_lines):
    """A match ending in a newline spans only the lines it ends; one more
    character reaches the next line, which must have been read too."""
    original = newline.join(["one", "", "three", "four", ""]).encode()
    matched = "one\n" + "\n" * (matched_lines - 1)

    def patched(old_string):
        target = tmp_path / "file.txt"
        target.write_bytes(original)
        context = ToolContext(cwd=tmp_path)
        fs.fs_read(str(target), start_line=1, end_line=matched_lines, context=context)
        return patch.patch(str(target), old_string=old_string, new_string="X\n", context=context)

    assert patched(matched).startswith("patched ")
    assert patched(matched + ("t" if matched_lines == 2 else "\n")).startswith("ERROR:")


@pytest.mark.parametrize("newline", [b"\n", b"\r\n", b"\r"])
def test_patch_line_including_newline_needs_only_that_line_read(tmp_path, newline):
    target = tmp_path / "file.txt"
    target.write_bytes(newline.join([b"one", b"two", b"three", b"four", b"five", b""]))
    context = ToolContext(cwd=tmp_path)
    fs.fs_read(str(target), start_line=1, end_line=1, context=context)

    result = patch.patch(str(target), old_string="one\n", new_string="A\nB\n", context=context)

    assert result.startswith("patched ")
    assert target.read_bytes() == newline.join([b"A", b"B", b"two", b"three", b"four", b"five", b""])
    assert context.reads.coverage(target).ranges == [(1, 2)]
    # Inserted text is known, but the formerly second line remains unread.
    assert patch.patch(str(target), old_string="B", new_string="BB", context=context).startswith("patched ")
    assert patch.patch(str(target), old_string="two", new_string="TWO", context=context).startswith("ERROR:")


@pytest.mark.parametrize("newline", [b"\n", b"\r\n", b"\r"])
def test_patch_that_reaches_following_unread_line_is_still_refused(tmp_path, newline):
    target = tmp_path / "file.txt"
    original = newline.join([b"one", b"two", b"three", b""])
    target.write_bytes(original)
    context = ToolContext(cwd=tmp_path)
    fs.fs_read(str(target), start_line=1, end_line=1, context=context)

    result = patch.patch(str(target), old_string="one\nt", new_string="changed", context=context)

    assert result.startswith("ERROR:")
    assert target.read_bytes() == original
    assert not context.undo.count(target)
