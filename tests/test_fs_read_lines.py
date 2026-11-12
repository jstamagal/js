"""Read pagination counts file newlines, not Unicode paragraph separators."""

import pytest

from js.toolkit import fs
from js.toolkit.core import ToolContext


@pytest.mark.parametrize("separator", ["\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"])
def test_read_preserves_non_newline_separators(tmp_path, separator):
    target = tmp_path / "source.txt"
    target.write_text(f"one{separator}two\nthree{separator}four\nfive\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    result = fs.read(str(target), context=context)

    assert result == f"1|one{separator}two\n2|three{separator}four\n3|five"
    assert context.read_ranges[target] == [(1, 3)]


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("ending", ["", "newline", "blank"])
def test_read_counts_only_real_lines(tmp_path, newline, ending):
    target = tmp_path / "source.txt"
    text = newline.join(["one", "two"])
    if ending:
        text += newline
    if ending == "blank":
        text += newline
    target.write_bytes(text.encode("utf-8"))
    context = ToolContext(cwd=tmp_path)

    result = fs.read(str(target), context=context)

    assert result == "1|one\n2|two" + ("\n3|" if ending == "blank" else "")
    assert context.read_ranges[target] == [(1, 3 if ending == "blank" else 2)]


@pytest.mark.parametrize("text, expected", [("\n", "1|"), ("\n\n", "1|\n2|")])
def test_read_blank_lines(tmp_path, text, expected):
    target = tmp_path / "blank.txt"
    target.write_bytes(text.encode("utf-8"))

    assert fs.read(str(target), context=ToolContext(cwd=tmp_path)) == expected


def test_read_empty_file_has_no_numbered_lines(tmp_path):
    target = tmp_path / "empty.txt"
    target.touch()
    context = ToolContext(cwd=tmp_path)

    result = fs.read(str(target), context=context)

    assert result == f"{target} is empty (hash {fs._hash_bytes(b'')})"
    assert not context.read_ranges.get(target)


def test_read_ranges_keep_physical_line_numbers(tmp_path):
    target = tmp_path / "source.txt"
    target.write_text("one\ftwo\nthree\u2028four\nfive\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path, max_read_lines=1)

    result = fs.read(str(target), start_line=2, context=context)

    assert result.startswith("2|three\u2028four\n[3 total lines;")
    assert '"start_line": 3' in result
    assert context.read_ranges[target] == [(2, 2)]
    assert fs.read(str(target), start_line=3, context=context) == "3|five"
