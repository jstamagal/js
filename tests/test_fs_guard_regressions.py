"""Read coverage must authorize only content actually displayed."""

import pytest

from js.toolkit import fs
from js.toolkit.core import ToolContext


@pytest.mark.parametrize("restart", [False, True])
def test_undo_restores_original_coverage(tmp_path, restart):
    target = tmp_path / "file.txt"
    original = "HEAD\n" + "".join(f"L{i:02d}\n" for i in range(2, 21))
    target.write_text(original)
    context = ToolContext(cwd=tmp_path)
    context.configure_snapshot_store("audit", tmp_path / "session.jsonl", state_dir=tmp_path / "state")
    fs.read(str(target), range={"start_line": 1, "end_line": 1}, context=context)
    assert fs.patch(str(target), old_string="HEAD", new_string="HEAD\n" + "owned\n" * 12, context=context).startswith("patched ")
    if restart:
        context = ToolContext(cwd=tmp_path)
        context.configure_snapshot_store("audit", tmp_path / "session.jsonl", state_dir=tmp_path / "state")
    assert fs.undo(str(target), context=context).startswith("restored ")
    assert context.read_ranges[target] == [(1, 1)]
    assert fs.patch(str(target), old_string="L11", new_string="ELEVEN", context=context).startswith("ERROR:")
    assert target.read_text() == original
    assert fs.patch(str(target), old_string="HEAD", new_string="head", context=context).startswith("patched ")


@pytest.mark.parametrize("separator", list("\v\f\x1c\x1d\x1e\x85\u2028\u2029"))
def test_patch_refuses_unsupported_line_coordinates(tmp_path, separator):
    target = tmp_path / "file.txt"
    original = f"one{separator}two{separator}three\n"
    target.write_text(original)
    context = ToolContext(cwd=tmp_path)
    shown = fs.read(str(target), range={"start_line": 1, "end_line": 1}, context=context)
    assert "three" not in shown
    result = fs.patch(str(target), old_string="three", new_string="THREE", context=context)
    assert result.startswith("ERROR: unsupported line separator")
    assert target.read_text() == original
    assert not context.snapshots