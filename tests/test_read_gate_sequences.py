"""Read -> patch -> write -> read-range -> undo sequences through the real tool
functions: which edits the read gate admits, and which it refuses and why."""

from __future__ import annotations

import json

from js.toolkit import ToolContext, build_default_registry, call_tool, fs
from js.toolkit.subagents import _child_context


def _lines(count: int) -> str:
    return "".join(f"line {n}\n" for n in range(1, count + 1))


def _outcome(result: str) -> str:
    if not result.startswith("ERROR"):
        return "accepted"
    if "must read the file" in result:
        return "unread"
    if "must read the target line" in result:
        return "unseen lines"
    if "since it was read" in result:
        return "changed since read"
    return result


def _patch(context: ToolContext, path, old: str, new: str) -> str:
    return _outcome(fs.patch(file_path=str(path), old_string=old, new_string=new, context=context))


def _read(context: ToolContext, path, start: int | None = None, end: int | None = None) -> None:
    result = fs.fs_read(file_path=str(path), start_line=start, end_line=end, context=context)
    assert not result.startswith("ERROR"), result


def _overwrite(context: ToolContext, path, content: str) -> str:
    return _outcome(fs.write(file_path=str(path), content=content, overwrite=True, context=context))


def _session_context(tmp_path, session: str) -> ToolContext:
    context = ToolContext(cwd=tmp_path)
    context.configure_snapshot_store("agent", tmp_path / "sessions" / f"{session}.jsonl",
                                     state_dir=tmp_path / "state")
    return context


def test_an_unread_file_refuses_patch_and_overwrite(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text(_lines(10))
    context = ToolContext(cwd=tmp_path)

    assert _patch(context, target, "line 2\n", "line two\n") == "unread"
    assert _overwrite(context, target, "new\n") == "unread"
    assert target.read_text() == _lines(10)


def test_a_ranged_read_admits_edits_to_its_lines_only(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text(_lines(10))
    context = ToolContext(cwd=tmp_path)

    _read(context, target, 1, 3)
    assert _patch(context, target, "line 2\n", "line two\n") == "accepted"
    assert _patch(context, target, "line 8\n", "line eight\n") == "unseen lines"
    _read(context, target, 6, 10)
    assert _patch(context, target, "line 8\n", "line eight\n") == "accepted"
    assert _patch(context, target, "line 5\n", "line five\n") == "unseen lines"


def test_a_long_old_string_is_its_own_proof(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("a line long enough to prove what it replaces\n" + _lines(3))
    context = ToolContext(cwd=tmp_path)

    assert _patch(context, target, "a line long enough to prove what it replaces\n", "short\n") == "accepted"


def test_any_page_admits_an_overwrite_and_the_written_file_counts_as_read(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text(_lines(10))
    context = ToolContext(cwd=tmp_path)

    _read(context, target, 1, 2)
    assert _overwrite(context, target, _lines(5)) == "accepted"
    assert _patch(context, target, "line 4\n", "line four\n") == "accepted"


def test_a_new_file_written_by_the_model_counts_as_read(tmp_path):
    target = tmp_path / "new.txt"
    context = ToolContext(cwd=tmp_path)

    assert _outcome(fs.write(file_path=str(target), content=_lines(4), context=context)) == "accepted"
    assert _patch(context, target, "line 3\n", "line three\n") == "accepted"


def test_undo_restores_the_coverage_of_the_restored_content(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text(_lines(10))
    context = ToolContext(cwd=tmp_path)

    _read(context, target, 1, 3)
    assert _patch(context, target, "line 2\n", "line two\n") == "accepted"
    assert _outcome(fs.undo(str(target), context=context)) == "accepted"
    assert target.read_text() == _lines(10)
    assert _patch(context, target, "line 3\n", "line three\n") == "accepted"
    assert _patch(context, target, "line 8\n", "line eight\n") == "unseen lines"


def test_undo_of_an_overwrite_brings_back_the_partial_coverage(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text(_lines(10))
    context = ToolContext(cwd=tmp_path)

    _read(context, target, 1, 2)
    assert _overwrite(context, target, "fresh\n") == "accepted"
    assert _outcome(fs.undo(str(target), context=context)) == "accepted"
    assert _patch(context, target, "line 1\n", "line one\n") == "accepted"
    assert _patch(context, target, "line 9\n", "line nine\n") == "unseen lines"


def test_a_change_on_disk_refuses_once_then_the_retry_goes_through(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text(_lines(40))
    context = ToolContext(cwd=tmp_path)

    _read(context, target)
    target.write_text(_lines(40).replace("line 5\n", "line five, edited elsewhere\n"))
    assert _patch(context, target, "line 30\n", "line thirty\n") == "changed since read"
    assert _patch(context, target, "line 30\n", "line thirty\n") == "accepted"
    assert _overwrite(context, target, "mine\n") == "accepted"


def test_an_overwrite_after_a_change_on_disk_is_refused_once(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text(_lines(3))
    context = ToolContext(cwd=tmp_path)

    _read(context, target, 1, 1)
    target.write_text("moved\n")
    assert _overwrite(context, target, "mine\n") == "changed since read"
    assert _overwrite(context, target, "mine\n") == "accepted"


def test_appended_lines_count_as_seen(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text(_lines(5))
    context = ToolContext(cwd=tmp_path)

    _read(context, target, 1, 2)
    appended = fs.write(file_path=str(target), content="added\n", append=True, context=context)
    assert _outcome(appended) == "accepted"
    assert _patch(context, target, "added\n", "grown\n") == "accepted"
    assert _patch(context, target, "line 2\n", "line two\n") == "accepted"
    assert _patch(context, target, "line 4\n", "line four\n") == "unseen lines"


def test_coverage_and_undo_survive_a_restart_on_the_same_session(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text(_lines(10))
    first = _session_context(tmp_path, "one")
    _read(first, target, 1, 3)
    assert _patch(first, target, "line 1\n", "line one\n") == "accepted"

    restarted = _session_context(tmp_path, "one")
    assert _patch(restarted, target, "line 2\n", "line two\n") == "accepted"
    assert _patch(restarted, target, "line 8\n", "line eight\n") == "unseen lines"
    assert _outcome(fs.undo(str(target), context=restarted)) == "accepted"
    assert _outcome(fs.undo(str(target), context=restarted)) == "accepted"
    assert target.read_text() == _lines(10)


def test_another_session_starts_without_coverage(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text(_lines(10))
    context = _session_context(tmp_path, "one")
    _read(context, target)

    context.configure_snapshot_store("agent", tmp_path / "sessions" / "two.jsonl", state_dir=tmp_path / "state")
    assert _patch(context, target, "line 2\n", "line two\n") == "unread"
    assert _outcome(fs.undo(str(target), context=context)) != "accepted"


def test_reads_before_the_first_session_carry_into_it(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text(_lines(10))
    context = ToolContext(cwd=tmp_path)
    _read(context, target, 1, 3)

    context.configure_snapshot_store("agent", tmp_path / "sessions" / "one.jsonl", state_dir=tmp_path / "state")
    assert _patch(context, target, "line 2\n", "line two\n") == "accepted"
    assert _patch(context, target, "line 8\n", "line eight\n") == "unseen lines"


def test_a_subagent_has_its_own_reads(tmp_path):
    parents_file = tmp_path / "parent.txt"
    parents_file.write_text(_lines(5))
    childs_file = tmp_path / "child.txt"
    childs_file.write_text(_lines(5))
    parent = ToolContext(cwd=tmp_path)
    _read(parent, parents_file)
    child = _child_context(parent, None, "worker")

    assert _patch(child, parents_file, "line 2\n", "line two\n") == "unread"
    _read(child, childs_file)
    assert _patch(parent, childs_file, "line 2\n", "line two\n") == "unread"
    assert _patch(child, childs_file, "line 2\n", "line two\n") == "accepted"


def test_with_file_require_read_off_nothing_is_gated(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text(_lines(10))
    context = ToolContext(cwd=tmp_path, file_require_read=False)

    assert _patch(context, target, "line 2\n", "line two\n") == "accepted"
    assert _overwrite(context, target, "new\n") == "accepted"


def test_a_notebook_cell_edit_needs_a_read_of_the_notebook(tmp_path):
    target = tmp_path / "nb.ipynb"
    target.write_text(json.dumps({
        "cells": [{"cell_type": "markdown", "id": "a1", "metadata": {}, "source": ["old"]}],
        "metadata": {}, "nbformat": 4, "nbformat_minor": 5,
    }))
    context = ToolContext(cwd=tmp_path)
    registry = build_default_registry()

    def edit() -> str:
        return _outcome(call_tool(registry.resolve("notebook_edit"),
                                  {"file_path": str(target), "cell_id": "a1", "new_source": "new"}, context))

    assert edit() == "unread"
    call_tool(registry.resolve("read"), {"file_path": str(target)}, context)
    assert edit() == "accepted"
    assert edit() == "accepted"
