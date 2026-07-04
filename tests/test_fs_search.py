"""fs_search (ripgrep-backed) + FIFO-safety coverage.

These exercise the rewrite of fs_search onto `rg` and the never-hang guarantees
around non-regular files (findings 63/64). They run offline; they require the
real `rg` binary, which the box provides and `just install` provisions.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading

import pytest

from js.toolkit import ToolContext
from js.toolkit import fs
from js.toolkit.fs import _iter_files, fs_read, fs_search, sem_search


requires_rg = pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")


def _call_with_timeout(fn, *args, timeout=10.0, **kwargs):
    """Run fn in a thread and fail if it does not return within `timeout`.

    A regression on the FIFO hole would block forever, so a plain call would
    hang the whole suite; this turns a hang into a test failure instead."""
    box: dict[str, object] = {}

    def run() -> None:
        box["result"] = fn(*args, **kwargs)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    assert not t.is_alive(), f"{getattr(fn, '__name__', fn)} did not return within {timeout}s (hang)"
    return box["result"]


@requires_rg
def test_fs_search_content_mode_returns_path_line_text(tmp_path):
    context = ToolContext(cwd=tmp_path)
    target = tmp_path / "code.py"
    target.write_text("class TaskRunner:\n    def run_task(self):\n        return 'x'\n", encoding="utf-8")

    actual = fs_search("TaskRunner", path=".", output_mode="content", context=context)

    assert f"{target}:1:class TaskRunner:" in actual


@requires_rg
def test_fs_search_files_with_matches_is_default(tmp_path):
    context = ToolContext(cwd=tmp_path)
    (tmp_path / "a.txt").write_text("needle here\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("nothing\n", encoding="utf-8")

    actual = fs_search("needle", path=".", context=context)

    assert str(tmp_path / "a.txt") in actual
    assert "b.txt" not in actual


@requires_rg
def test_fs_search_count_mode_reports_per_file_line_counts(tmp_path):
    context = ToolContext(cwd=tmp_path)
    (tmp_path / "a.txt").write_text("hit\nhit\nmiss\n", encoding="utf-8")

    actual = fs_search("hit", path=".", output_mode="count", context=context)

    assert actual == f"{tmp_path / 'a.txt'}:2"


@requires_rg
def test_fs_search_deduplicates_repeated_search(tmp_path):
    context = ToolContext(cwd=tmp_path)
    (tmp_path / "a.txt").write_text("token\n", encoding="utf-8")

    first = fs_search("token", path=".", output_mode="content", context=context)
    second = fs_search("token", path=".", output_mode="content", context=context)

    assert second == first + "\n[deduplicated repeated search]"


@requires_rg
def test_fs_search_no_matches_returns_clean_marker(tmp_path):
    context = ToolContext(cwd=tmp_path)
    (tmp_path / "a.txt").write_text("nothing relevant\n", encoding="utf-8")

    assert fs_search("zzz_absent_zzz", path=".", context=context) == "(no matches)"


@requires_rg
def test_fs_search_invalid_regex_degrades_without_traceback(tmp_path):
    context = ToolContext(cwd=tmp_path)
    (tmp_path / "a.txt").write_text("data\n", encoding="utf-8")

    actual = fs_search("(", path=".", context=context)

    assert actual.startswith("ERROR:")
    assert "(no matches)" != actual


@requires_rg
def test_fs_search_head_limit_and_offset_slice_results(tmp_path):
    context = ToolContext(cwd=tmp_path)
    (tmp_path / "a.txt").write_text("m\n" * 5, encoding="utf-8")

    page = fs_search("m", path="a.txt", output_mode="content", head_limit=2, offset=1, context=context)

    lines = page.splitlines()
    assert len(lines) == 2
    assert lines[0] == f"{tmp_path / 'a.txt'}:2:m"


@requires_rg
def test_fs_search_respects_gitignore_in_git_repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (tmp_path / "kept.txt").write_text("needle\n", encoding="utf-8")
    (tmp_path / "ignored.txt").write_text("needle\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    actual = fs_search("needle", path=".", context=context)

    assert str(tmp_path / "kept.txt") in actual
    assert "ignored.txt" not in actual


@requires_rg
def test_fs_search_respects_dot_ignore_file_without_git(tmp_path):
    (tmp_path / ".ignore").write_text("ignored.txt\n", encoding="utf-8")
    (tmp_path / "kept.txt").write_text("needle\n", encoding="utf-8")
    (tmp_path / "ignored.txt").write_text("needle\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    actual = fs_search("needle", path=".", context=context)

    assert str(tmp_path / "kept.txt") in actual
    assert "ignored.txt" not in actual


@requires_rg
def test_fs_search_skips_binary_files(tmp_path):
    context = ToolContext(cwd=tmp_path)
    (tmp_path / "good.txt").write_text("needle here\n", encoding="utf-8")
    (tmp_path / "bad.bin").write_bytes(b"needle\x00binary\n")

    actual = fs_search("needle", path=".", context=context)

    assert str(tmp_path / "good.txt") in actual
    assert "bad.bin" not in actual


@requires_rg
def test_fs_search_over_directory_with_fifo_does_not_hang(tmp_path):
    context = ToolContext(cwd=tmp_path)
    (tmp_path / "real.txt").write_text("needle\n", encoding="utf-8")
    os.mkfifo(tmp_path / "pipe")

    actual = _call_with_timeout(fs_search, "needle", path=".", context=context, timeout=10.0)

    assert str(tmp_path / "real.txt") in actual


def test_fs_search_missing_rg_binary_degrades_cleanly(tmp_path, monkeypatch):
    context = ToolContext(cwd=tmp_path)
    (tmp_path / "a.txt").write_text("needle\n", encoding="utf-8")
    monkeypatch.setattr(fs.shutil, "which", lambda _cmd: None)

    actual = fs_search("needle", path=".", context=context)

    assert actual == fs._RG_MISSING


def test_iter_files_skips_fifo_and_yields_regular_only(tmp_path):
    (tmp_path / "real.txt").write_text("x\n", encoding="utf-8")
    os.mkfifo(tmp_path / "pipe")

    found = _call_with_timeout(lambda: list(_iter_files(tmp_path)), timeout=10.0)

    names = {p.name for p in found}
    assert names == {"real.txt"}


def test_sem_search_over_directory_with_fifo_does_not_hang(tmp_path):
    context = ToolContext(cwd=tmp_path)
    (tmp_path / "mod.py").write_text("def handler():\n    return 1\n", encoding="utf-8")
    os.mkfifo(tmp_path / "pipe")

    actual = _call_with_timeout(
        sem_search, [{"query": "handler", "path": ".", "glob": "*.py"}], context=context, timeout=10.0
    )

    assert "mod.py" in actual


def test_fs_read_of_fifo_refuses_without_hanging(tmp_path):
    context = ToolContext(cwd=tmp_path)
    os.mkfifo(tmp_path / "pipe")

    actual = _call_with_timeout(fs_read, path="pipe", context=context, timeout=10.0)

    assert actual.startswith("ERROR: not a regular file")


def test_fs_read_empty_file_message_uses_resolved_path(tmp_path):
    context = ToolContext(cwd=tmp_path)
    empty = tmp_path / "empty.txt"
    empty.write_text("", encoding="utf-8")

    # Read via the `path=` alias: the empty-file message must name the resolved
    # target, never the literal "None" that file_path holds on this path.
    actual = fs_read(path="empty.txt", context=context)

    assert actual == f"{empty} is empty (hash {fs._hash_bytes(b'')})"
    assert "None" not in actual
