"""The fs_search dedup cache must not outlive the tree it describes.

The cache is keyed only on the search arguments. Without invalidation, a model
that edits a file and re-runs the same search gets the PRE-EDIT hit list back
labelled `[deduplicated repeated search]`, which reads as confirmation that
nothing changed.
"""
from __future__ import annotations

import shutil

import pytest

from js.toolkit import ToolContext
from js.toolkit.fs import fs_read, fs_search, patch
from js.toolkit.process_net import shell


requires_rg = pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")


@requires_rg
def test_editing_a_file_makes_the_next_identical_search_see_the_edit(tmp_path):
    target = tmp_path / "sub.py"
    target.write_text("MARKER_ALPHA = 1\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    first = fs_search("MARKER_ALPHA", path=str(tmp_path), output_mode="content", context=context)
    assert "MARKER_ALPHA" in first

    fs_read(str(target), context=context)
    patch(
        str(target),
        old_string="MARKER_ALPHA = 1",
        new_string="MARKER_BETA = 1",
        context=context,
    )

    again = fs_search("MARKER_ALPHA", path=str(tmp_path), output_mode="content", context=context)
    assert "deduplicated" not in again
    assert "MARKER_ALPHA" not in again


@requires_rg
def test_a_shell_command_makes_the_next_identical_search_see_its_writes(tmp_path):
    context = ToolContext(cwd=tmp_path)
    (tmp_path / "seed.txt").write_text("nothing here\n", encoding="utf-8")

    first = fs_search("MARKER_GAMMA", path=str(tmp_path), context=context)
    assert "MARKER_GAMMA" not in first

    shell("printf 'MARKER_GAMMA\\n' > made-by-shell.txt", cwd=str(tmp_path), context=context)

    again = fs_search("MARKER_GAMMA", path=str(tmp_path), context=context)
    assert "deduplicated" not in again
    assert "made-by-shell.txt" in again
