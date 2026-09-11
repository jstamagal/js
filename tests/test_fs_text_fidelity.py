"""Byte-level regressions for write/patch newline and UTF-8 contracts."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from js.toolkit import fs
from js.toolkit.core import ToolContext


@pytest.mark.parametrize("newline", [b"\n", b"\r\n", b"\r"], ids=["lf", "crlf", "cr"])
def test_write_preserves_existing_newlines(tmp_path, newline):
    target = tmp_path / "file.txt"
    original = newline.join([b"a", b"b", b""])
    target.write_bytes(original)
    context = ToolContext(cwd=tmp_path)
    assert not fs.read(str(target), context=context).startswith("ERROR")

    result = fs.write(str(target), "A\nB\n", overwrite=True, context=context)

    assert result.startswith("wrote ")
    assert target.read_bytes() == newline.join([b"A", b"B", b""])
    assert fs.undo(str(target), context=context).startswith("restored ")
    assert target.read_bytes() == original


@pytest.mark.parametrize("newline", [b"\n", b"\r\n", b"\r"], ids=["lf", "crlf", "cr"])
def test_patch_preserves_unedited_bytes(tmp_path, newline):
    target = tmp_path / "file.txt"
    original = newline.join([b"alpha", b"beta", b"gamma", b""])
    target.write_bytes(original)
    context = ToolContext(cwd=tmp_path)
    assert not fs.read(str(target), context=context).startswith("ERROR")

    result = fs.patch(str(target), old_string="beta\n", new_string="BETA\n", context=context)

    assert result.startswith("patched ")
    assert target.read_bytes() == original.replace(b"beta", b"BETA")
    assert fs.undo(str(target), context=context).startswith("restored ")
    assert target.read_bytes() == original


@pytest.mark.parametrize("original", [b"alpha\rbeta\rgamma\r", b"alpha\r\nbeta\ngamma\r\n"])
def test_cr_patch_requires_read_of_actual_target_line(tmp_path, original):
    target = tmp_path / "file.txt"
    target.write_bytes(original)
    context = ToolContext(cwd=tmp_path)
    fs.read(str(target), start_line=1, end_line=1, context=context)

    result = fs.patch(str(target), old_string="gamma", new_string="GAMMA", context=context)

    assert result == "ERROR: You must read the target line (3) before attempting to edit it."
    assert target.read_bytes() == original


def test_cr_patch_tracks_read_ranges_after_inserting_lines(tmp_path):
    target = tmp_path / "file.txt"
    target.write_bytes(b"alpha\rbeta\rgamma\r")
    context = ToolContext(cwd=tmp_path)
    fs.read(str(target), start_line=2, end_line=2, context=context)

    result = fs.patch(
        str(target),
        edits=[
            {"old_string": "beta", "new_string": "one\ntwo"},
            {"old_string": "two", "new_string": "TWO"},
        ],
        context=context,
    )

    assert result.startswith("patched ")
    assert target.read_bytes() == b"alpha\rone\rTWO\rgamma\r"
    assert context.read_ranges[target] == [(2, 3)]
    result = fs.patch(str(target), old_string="gamma", new_string="GAMMA", context=context)
    assert result == "ERROR: You must read the target line (4) before attempting to edit it."


@pytest.mark.parametrize("newline", [b"\r\n", b"\r"], ids=["crlf", "cr"])
def test_failed_patch_batch_preserves_bytes_and_snapshot_state(tmp_path, newline):
    target = tmp_path / "file.txt"
    original = newline.join([b"alpha", b"beta", b""])
    target.write_bytes(original)
    context = ToolContext(cwd=tmp_path)
    fs.read(str(target), context=context)
    hashes = dict(context.file_hashes)
    ranges = dict(context.read_ranges)

    result = fs.patch(
        str(target),
        edits=[
            {"old_string": "alpha\n", "new_string": "ALPHA\n"},
            {"old_string": "missing", "new_string": "replacement"},
        ],
        context=context,
    )

    assert result.startswith("ERROR: edit 2: Could not find match")
    assert target.read_bytes() == original
    assert context.snapshots == {}
    assert context.file_hashes == hashes
    assert context.read_ranges == ranges


def test_patch_refuses_an_ambiguous_overlapping_match(tmp_path):
    target = tmp_path / "file.txt"
    original = "ababa\n"
    target.write_text(original)
    context = ToolContext(cwd=tmp_path)
    fs.read(str(target), context=context)

    result = fs.patch(str(target), old_string="aba", new_string="X", context=context)

    assert result.startswith("ERROR: Multiple matches found")
    assert target.read_text() == original
    assert not context.snapshots


def test_patch_replace_all_rewrites_non_overlapping_occurrences(tmp_path):
    target = tmp_path / "file.txt"
    target.write_text("ababa\n")
    context = ToolContext(cwd=tmp_path)
    fs.read(str(target), context=context)

    result = fs.patch(
        str(target), old_string="aba", new_string="X", replace_all=True, context=context
    )

    assert result.startswith("patched ")
    assert target.read_text() == "Xba\n"


def test_patch_batch_aborts_when_an_edit_normalizes_to_a_noop(tmp_path):
    target = tmp_path / "file.txt"
    original = b"alpha\nbeta\n"
    target.write_bytes(original)
    context = ToolContext(cwd=tmp_path)
    fs.read(str(target), context=context)

    result = fs.patch(
        str(target),
        edits=[
            {"old_string": "alpha\n", "new_string": "ALPHA\n"},
            {"old_string": "beta\r\n", "new_string": "beta\n"},
        ],
        context=context,
    )

    assert result.startswith("ERROR: edit 2:")
    assert target.read_bytes() == original
    assert not context.snapshots


@pytest.mark.parametrize("operation", ["patch", "write"])
def test_utf8_edit_under_ascii_locale(tmp_path, operation):
    script = r'''
import codecs
import locale
import sys
from pathlib import Path
from js.toolkit import fs
from js.toolkit.core import ToolContext

assert codecs.lookup(locale.getpreferredencoding(False)).name == "ascii"
target = Path(sys.argv[1]) / "file.txt"
target.write_bytes(b"caf\xc3\xa9\r\n")
context = ToolContext(cwd=target.parent)
result = fs.read(str(target), context=context)
assert not result.startswith("ERROR"), ascii(result)
if sys.argv[2] == "patch":
    result = fs.patch(str(target), old_string="caf\u00e9", new_string="th\u00e9", context=context)
    assert result.startswith("patched "), ascii(result)
else:
    result = fs.write(str(target), "th\u00e9\n", overwrite=True, context=context)
    assert result.startswith("wrote "), ascii(result)
assert target.read_bytes() == b"th\xc3\xa9\r\n", repr(target.read_bytes())
'''
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), operation],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "LC_ALL": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0"},
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
