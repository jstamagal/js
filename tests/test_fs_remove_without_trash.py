"""remove(permanent=true) on a box with no trash: it deletes, and undo restores."""
from __future__ import annotations

import pytest

from js.toolkit import ToolContext, build_default_registry, call_tool
from js.toolkit import fs


@pytest.fixture
def trashless_box(tmp_path, monkeypatch):
    """HOME with no ~/.local/share/Trash and a PATH with no trash command."""
    home = tmp_path / "home"
    home.mkdir()
    empty_bin = tmp_path / "bin"
    empty_bin.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("PATH", str(empty_bin))
    assert fs._trash_command() is None
    assert not (home / ".local" / "share" / "Trash").exists()
    return tmp_path


def _context(root, *, persistent: bool) -> ToolContext:
    context = ToolContext(cwd=root)
    if persistent:
        context.configure_snapshot_store(
            "test-agent", root / "sessions" / "trashless.jsonl", state_dir=root / "state"
        )
    return context


def _remove_tool():
    return build_default_registry().resolve("remove")


@pytest.mark.parametrize("permanent", [True, "true"])
@pytest.mark.parametrize("restart", [False, True])
def test_permanent_remove_deletes_file_without_trash_and_undo_restores(trashless_box, permanent, restart):
    work = trashless_box / "work"
    work.mkdir()
    target = work / "notes.txt"
    target.write_bytes(b"keep me\n")
    context = _context(trashless_box, persistent=True)

    result = call_tool(_remove_tool(), {"path": str(target), "permanent": permanent}, context)

    assert not result.startswith("ERROR"), result
    assert not target.exists()
    if restart:
        context = _context(trashless_box, persistent=True)
    restored = fs.undo(str(target), context=context)
    assert not restored.startswith("ERROR"), restored
    assert target.read_bytes() == b"keep me\n"


def test_permanent_remove_deletes_directory_without_trash_and_undo_restores(trashless_box):
    tree = trashless_box / "tree"
    (tree / "nested").mkdir(parents=True)
    (tree / "nested" / "child.txt").write_bytes(b"child\n")
    context = _context(trashless_box, persistent=False)

    result = fs.remove(str(tree), permanent=True, context=context)

    assert not result.startswith("ERROR"), result
    assert not tree.exists()
    assert not fs.undo(str(tree), context=context).startswith("ERROR")
    assert (tree / "nested" / "child.txt").read_bytes() == b"child\n"


def test_trash_remove_without_trash_refuses_and_leaves_the_file(trashless_box):
    target = trashless_box / "notes.txt"
    target.write_bytes(b"keep me\n")
    context = _context(trashless_box, persistent=False)

    result = fs.remove(str(target), context=context)

    assert result.startswith("ERROR")
    assert target.read_bytes() == b"keep me\n"
    assert not context.snapshots.get(target)
