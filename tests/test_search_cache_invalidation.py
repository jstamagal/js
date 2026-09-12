"""The fs_search and ast_search dedup caches must not outlive the tree they
describe.

Only a regular-file root is memoized, keyed on the search arguments plus the
root's stat; a directory root is never memoized, because its own stat does not
move when a nested file changes. Without invalidation, a model that edits a file
and re-runs the same search gets the PRE-EDIT hit list back labelled
`[deduplicated repeated search]`, which reads as confirmation that nothing
changed.
"""
from __future__ import annotations

import shutil

import pytest

from js.toolkit import fs
from js.toolkit import ToolContext
from js.toolkit.fs import ast_search, fs_read, fs_search, patch, undo
from js.toolkit.process_net import shell


requires_rg = pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
requires_ast_grep = pytest.mark.skipif(
    fs._ast_grep_binary() is None, reason="ast-grep 0.45.1 not installed"
)


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


@requires_rg
def test_undo_makes_the_next_identical_search_see_the_restored_bytes(tmp_path):
    target = tmp_path / "file.txt"
    target.write_text("before", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)
    fs_read(str(target), context=context)
    patch(str(target), old_string="before", new_string="after", context=context)
    assert "after" in fs_search("after", path=str(target), output_mode="content", context=context)

    assert undo(str(target), context=context).startswith("restored ")
    again = fs_search("after", path=str(target), output_mode="content", context=context)

    assert "deduplicated" not in again
    assert "after" not in again
    assert target.read_text(encoding="utf-8") == "before"


@requires_rg
def test_an_external_write_makes_the_next_identical_search_see_it(tmp_path):
    target = tmp_path / "file.txt"
    target.write_text("before\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)
    assert "before" in fs_search("before", path=str(target), output_mode="content", context=context)

    target.write_text("external edit\n", encoding="utf-8")
    again = fs_search("before", path=str(target), output_mode="content", context=context)

    assert "deduplicated" not in again
    assert "before" not in again


@requires_rg
def test_a_directory_root_is_not_memoized(tmp_path):
    (tmp_path / "a.txt").write_text("NEEDLE\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    first = fs_search("NEEDLE", path=str(tmp_path), output_mode="content", context=context)
    second = fs_search("NEEDLE", path=str(tmp_path), output_mode="content", context=context)

    assert "NEEDLE" in first
    assert "deduplicated" not in second


@requires_rg
def test_a_nested_external_write_makes_a_directory_root_search_see_it(tmp_path):
    target = tmp_path / "sub" / "a.txt"
    target.parent.mkdir()
    target.write_text("NEEDLE before\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    first = fs_search("NEEDLE before", path=str(tmp_path), output_mode="content", context=context)
    assert "NEEDLE before" in first

    target.write_text("gone\n", encoding="utf-8")

    again = fs_search("NEEDLE before", path=str(tmp_path), output_mode="content", context=context)
    assert "deduplicated" not in again
    assert "NEEDLE before" not in again


@requires_ast_grep
def test_an_unchanged_ast_search_file_root_is_still_deduplicated(tmp_path):
    target = tmp_path / "a.py"
    target.write_text("def alpha():\n    return 1\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    first = ast_search("def $F(): $$$", path=str(target), context=context)
    again = ast_search("def $F(): $$$", path=str(target), context=context)

    assert "alpha" in first
    assert again == first + "\n[deduplicated repeated search]"


@requires_ast_grep
def test_an_external_write_makes_the_next_identical_ast_search_see_it(tmp_path):
    target = tmp_path / "a.py"
    target.write_text("def alpha():\n    return 1\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)
    assert "alpha" in ast_search("def $F(): $$$", path=str(target), context=context)

    target.write_text("def beta():\n    return 2\n", encoding="utf-8")
    again = ast_search("def $F(): $$$", path=str(target), context=context)

    assert "deduplicated" not in again
    assert "beta" in again
    assert "alpha" not in again


@requires_ast_grep
def test_a_directory_root_is_not_memoized_for_ast_search(tmp_path):
    (tmp_path / "a.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    first = ast_search("def $F(): $$$", path=str(tmp_path), context=context)
    second = ast_search("def $F(): $$$", path=str(tmp_path), context=context)

    assert "alpha" in first
    assert "deduplicated" not in second


@requires_ast_grep
def test_a_nested_external_write_makes_an_ast_search_directory_root_see_it(tmp_path):
    target = tmp_path / "sub" / "a.py"
    target.parent.mkdir()
    target.write_text("def alpha():\n    return 1\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)
    assert "alpha" in ast_search("def $F(): $$$", path=str(tmp_path), context=context)

    target.write_text("def beta():\n    return 2\n", encoding="utf-8")
    again = ast_search("def $F(): $$$", path=str(tmp_path), context=context)

    assert "deduplicated" not in again
    assert "beta" in again
    assert "alpha" not in again
