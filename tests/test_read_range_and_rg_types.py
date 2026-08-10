"""read()'s window resolution and fs_search's ripgrep type mapping."""
from __future__ import annotations

import shutil
import threading
import time

import pytest

from js.toolkit import ToolContext
from js.toolkit.fs import _rg_stream, _rg_types, fs_read, fs_search


requires_rg = pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")


def _numbered_lines(body: str) -> list[int]:
    """Line numbers out of read()'s `<number>:<2-char checksum>|<text>` prefix."""
    out = []
    for line in body.splitlines():
        head, sep, _rest = line.partition("|")
        if not sep or len(head) < 3:
            continue
        number, hash_sep, line_hash = head.partition(":")
        if hash_sep and len(line_hash) == 2 and number.isdigit():
            out.append(int(number))
    return out


def test_omitting_end_line_pages_from_start_line_not_from_line_one(tmp_path):
    target = tmp_path / "long.txt"
    target.write_text("\n".join(f"line{i}" for i in range(1, 61)), encoding="utf-8")
    context = ToolContext(cwd=tmp_path, max_read_lines=10)

    body = fs_read(str(target), start_line=40, context=context)
    assert not body.startswith("ERROR:"), body
    seen = _numbered_lines(body)
    assert seen[0] == 40
    assert seen[-1] == 49


def test_a_reversed_range_is_swapped_rather_than_rejected(tmp_path):
    target = tmp_path / "long.txt"
    target.write_text("\n".join(f"line{i}" for i in range(1, 31)), encoding="utf-8")
    context = ToolContext(cwd=tmp_path, max_read_lines=100)

    body = fs_read(str(target), start_line=20, end_line=12, context=context)
    assert not body.startswith("ERROR:"), body
    seen = _numbered_lines(body)
    assert seen[0] == 12
    assert seen[-1] == 20


def test_an_oversized_range_is_clamped_rather_than_rejected(tmp_path):
    target = tmp_path / "long.txt"
    target.write_text("\n".join(f"line{i}" for i in range(1, 101)), encoding="utf-8")
    context = ToolContext(cwd=tmp_path, max_read_lines=10)

    body = fs_read(str(target), start_line=1, end_line=100, context=context)
    assert not body.startswith("ERROR:"), body
    assert len(_numbered_lines(body)) == 10


@requires_rg
def test_a_ripgrep_type_name_matches_that_type_not_a_literal_extension(tmp_path):
    (tmp_path / "hit.rs").write_text("fn marker_delta() {}\n", encoding="utf-8")
    (tmp_path / "miss.txt").write_text("marker_delta\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    assert "rust" in _rg_types(shutil.which("rg"))
    out = fs_search("marker_delta", path=str(tmp_path), file_type="rust", context=context)
    assert "hit.rs" in out
    assert "miss.txt" not in out


@requires_rg
def test_an_extension_ripgrep_has_no_type_for_still_matches(tmp_path):
    (tmp_path / "shader.gdshader").write_text("marker_epsilon\n", encoding="utf-8")
    (tmp_path / "other.txt").write_text("marker_epsilon\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    assert "gdshader" not in _rg_types(shutil.which("rg"))
    out = fs_search("marker_epsilon", path=str(tmp_path), file_type="gdshader", context=context)
    assert "shader.gdshader" in out
    assert "other.txt" not in out


def test_a_scan_that_emits_nothing_is_still_killed_at_the_deadline():
    """The old deadline lived inside `for line in proc.stdout`, which blocks until
    the child emits something — so it could only fire while output was already
    flowing, exactly the case that does not need a timeout. A search that matches
    nothing emits no lines at all and ran unbounded.

    The child here is a plain sleep rather than a real ripgrep scan: a scan slow
    enough to outlive the deadline on a cold page cache finishes well inside it on
    a warm one, which makes the timing the test's subject rather than the
    watchdog. This produces no output for a known duration every time."""
    box: dict[str, object] = {}

    def run() -> None:
        started = time.monotonic()
        box["result"] = _rg_stream(["sh", "-c", "sleep 30"], want=1000, timeout=1)
        box["elapsed"] = time.monotonic() - started

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(15)
    assert not thread.is_alive(), "_rg_stream did not honour its deadline"
    lines, _rc, _stderr, timed_out = box["result"]
    assert timed_out is True
    assert lines == []
    assert box["elapsed"] < 10
