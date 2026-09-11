from __future__ import annotations

from pathlib import Path
import stat

import pytest

from js.toolkit import ToolContext, build_default_registry, call_tool
from js.toolkit import core, fs


def _persistent_context(tmp_path, session_name: str) -> ToolContext:
    context = ToolContext(cwd=tmp_path)
    context.configure_snapshot_store(
        "test-agent",
        tmp_path / "sessions" / f"{session_name}.jsonl",
        state_dir=tmp_path / "state",
    )
    return context


@pytest.mark.parametrize("restart", [False, True])
def test_removed_tree_preserves_links_and_modes(tmp_path, restart):
    tree = tmp_path / "tree"
    nested = tree / "real" / "nested"
    nested.mkdir(parents=True)
    secret = nested / "secret"
    secret.write_bytes(b"private")
    secret.chmod(0o600)
    nested.chmod(0o750)
    tree.chmod(0o700)
    links = {"file-link": "real/nested/secret", "dir-link": "real", "broken": "missing", "real/back": ".."}
    for name, destination in links.items():
        (tree / name).symlink_to(destination)
    context = _persistent_context(tmp_path, "metadata")
    assert fs.remove(str(tree), permanent=True, context=context).startswith("removed")
    if restart:
        context = _persistent_context(tmp_path, "metadata")
    assert fs.undo(str(tree), context=context).startswith("restored directory")
    for name, destination in links.items():
        assert (tree / name).is_symlink()
        assert (tree / name).readlink() == Path(destination)
    assert secret.read_bytes() == b"private"
    for path, mode in [(secret, 0o600), (nested, 0o750), (tree, 0o700)]:
        assert stat.S_IMODE(path.stat().st_mode) == mode


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("mode", [0o600, 0o751])
def test_removed_file_preserves_mode(tmp_path, restart, mode):
    target = tmp_path / "secret"
    target.write_bytes(b"private")
    target.chmod(mode)
    context = _persistent_context(tmp_path, "mode")
    assert fs.remove(str(target), permanent=True, context=context).startswith("removed")
    if restart:
        context = _persistent_context(tmp_path, "mode")
    assert fs.undo(str(target), context=context).startswith("restored")
    assert target.read_bytes() == b"private"
    assert stat.S_IMODE(target.stat().st_mode) == mode


@pytest.mark.parametrize("directory", [False, True])
def test_mode_restore_failure_retains_persisted_snapshot(tmp_path, monkeypatch, directory):
    target = tmp_path / "target"
    if directory:
        target.mkdir()
    secret = target / "secret" if directory else target
    secret.write_bytes(b"private")
    secret.chmod(0o600)
    context = _persistent_context(tmp_path, "chmod-retry")
    fs.remove(str(target), permanent=True, context=context)

    def fail(*args, **kwargs):
        raise PermissionError("injected chmod failure")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "chmod", fail)
        assert fs.undo(str(target), context=context) == "ERROR: injected chmod failure"
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600
    context = _persistent_context(tmp_path, "chmod-retry")
    assert fs.undo(str(target), context=context).startswith("restored")
    assert secret.read_bytes() == b"private"
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600


def test_legacy_directory_snapshot_still_restores(tmp_path):
    target = tmp_path / "tree"
    context = _persistent_context(tmp_path, "legacy")
    context.record_snapshot(target, {"kind": "directory", "entries": {"nested/": None, "nested/file": b"old"}})
    context = _persistent_context(tmp_path, "legacy")
    assert fs.undo(str(target), context=context).startswith("restored directory")
    assert (target / "nested/file").read_bytes() == b"old"


def test_persisted_tree_rejects_writes_through_symlink(tmp_path):
    target = tmp_path / "tree"
    context = _persistent_context(tmp_path, "unsafe")
    context.record_snapshot(target, {"kind": "directory", "entries": {
        "link": {"kind": "symlink", "target": str(tmp_path)}, "link/escaped": b"bad",
    }})
    context = _persistent_context(tmp_path, "unsafe")
    assert fs.undo(str(target), context=context).startswith("ERROR: discarded unusable snapshot")
    assert not (tmp_path / "escaped").exists()


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("directory", [False, True])
def test_capture_failure_never_authorizes_deletion(tmp_path, monkeypatch, restart, directory):
    target = tmp_path / "target"
    if directory:
        target.mkdir()
    file = target / "child" if directory else target
    file.write_text("original")
    context = _persistent_context(tmp_path, "capture")
    context.snapshot(target)
    file.write_text("must survive")
    real_read = Path.read_bytes

    def failing_read(path):
        if path == file:
            raise PermissionError("injected capture failure")
        return real_read(path)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_bytes", failing_read)
        context.snapshot(target)
    if restart:
        context = _persistent_context(tmp_path, "capture")

    result = fs.undo(str(target), context=context)
    assert result.startswith("ERROR: discarded unusable snapshot")
    assert "injected capture failure" in result
    assert file.read_text() == "must survive"
    assert fs.undo(str(target), context=context).startswith("restored")
    assert file.read_text() == "original"


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("kind", ["file", "directory", "symlink", "missing"])
def test_failed_undo_retains_snapshot_for_retry(tmp_path, monkeypatch, restart, kind):
    target = tmp_path / "target"
    context = _persistent_context(tmp_path, "retry")
    if kind == "file":
        target.write_text("original")
    elif kind == "directory":
        target.mkdir()
        (target / "child").write_text("original")
    elif kind == "symlink":
        target.symlink_to("original")
    fs._snapshot_remove_target(context, target)
    if kind == "directory":
        (target / "child").write_text("changed")
    else:
        if kind == "symlink":
            target.unlink()
        target.write_text("changed")
    entries = list(context.snapshot_files[target])
    method = {"file": "write_bytes", "directory": "write_bytes", "symlink": "symlink_to", "missing": "unlink"}[kind]

    def fail(*args, **kwargs):
        raise PermissionError("injected restore failure")

    with monkeypatch.context() as patch:
        patch.setattr(Path, method, fail)
        assert fs.undo(str(target), context=context) == "ERROR: injected restore failure"
    assert len(context.snapshots[target]) == 1
    assert context.snapshot_files[target] == entries
    assert all(entry.is_file() for entry in entries)
    if restart:
        context = _persistent_context(tmp_path, "retry")
    assert fs.undo(str(target), context=context).startswith("restored")
    if kind == "missing":
        assert not target.exists()
    elif kind == "symlink":
        assert target.is_symlink()
        assert target.readlink() == Path("original")
    else:
        assert (target / "child" if kind == "directory" else target).read_text() == "original"
    assert not context.snapshots[target]
    assert all(not entry.exists() for entry in entries)
    assert not _persistent_context(tmp_path, "retry").snapshots.get(target)


def test_undo_restores_a_patch_after_context_restart(tmp_path):
    target = tmp_path / "restart.txt"
    target.write_text("before\n", encoding="utf-8")
    first_process = _persistent_context(tmp_path, "restart")
    fs.read("restart.txt", context=first_process)
    patched = fs.patch(
        file_path="restart.txt",
        old_string="before",
        new_string="after",
        context=first_process,
    )

    second_process = _persistent_context(tmp_path, "restart")
    restored = fs.undo("restart.txt", context=second_process)

    assert patched.startswith(f"patched {target}")
    assert restored.startswith(f"restored {target}")
    assert target.read_text(encoding="utf-8") == "before\n"


def test_snapshot_history_is_scoped_to_its_session(tmp_path):
    target = tmp_path / "isolated.txt"
    target.write_text("before\n", encoding="utf-8")
    session_a = _persistent_context(tmp_path, "session-a")
    fs.read("isolated.txt", context=session_a)
    fs.patch(
        file_path="isolated.txt",
        old_string="before",
        new_string="after",
        context=session_a,
    )

    session_b = _persistent_context(tmp_path, "session-b")
    result = fs.undo("isolated.txt", context=session_b)

    assert result == f"ERROR: no snapshot available for {target}"
    assert target.read_text(encoding="utf-8") == "after\n"


def test_corrupt_persisted_snapshot_costs_one_undo_entry(tmp_path):
    target = tmp_path / "corrupt.txt"
    target.write_text("zero\n", encoding="utf-8")
    first_process = _persistent_context(tmp_path, "corrupt")
    fs.read("corrupt.txt", context=first_process)
    fs.patch(file_path="corrupt.txt", old_string="zero", new_string="one", context=first_process)
    fs.patch(file_path="corrupt.txt", old_string="one", new_string="two", context=first_process)
    snapshot_files = sorted(first_process.snapshot_store.glob("*/*.snapshot"))
    snapshot_files[-1].write_bytes(b"broken snapshot")

    second_process = _persistent_context(tmp_path, "corrupt")
    discarded = fs.undo("corrupt.txt", context=second_process)
    restored = fs.undo("corrupt.txt", context=second_process)

    assert discarded.startswith(f"ERROR: discarded unusable snapshot for {target}:")
    assert restored.startswith(f"restored {target}")
    assert target.read_text(encoding="utf-8") == "zero\n"


def test_snapshot_store_evicts_oldest_entry_at_count_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "_SNAPSHOT_MAX_ENTRIES", 2)
    target = tmp_path / "bounded.txt"
    context = _persistent_context(tmp_path, "bounded")
    for body in ("zero\n", "one\n", "two\n"):
        target.write_text(body, encoding="utf-8")
        context.snapshot(target)

    persisted = sorted(context.snapshot_store.glob("*/*.snapshot"))
    restarted = _persistent_context(tmp_path, "bounded")
    target.write_text("three\n", encoding="utf-8")
    latest = fs.undo("bounded.txt", context=restarted)
    older = fs.undo("bounded.txt", context=restarted)

    assert len(persisted) == 2
    assert latest.startswith(f"restored {target}")
    assert older.startswith(f"restored {target}")
    assert target.read_text(encoding="utf-8") == "one\n"


def test_oversized_snapshot_stays_bounded_and_warns_that_restart_undo_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "_SNAPSHOT_MAX_DISK_BYTES", 1024 * 1024)
    target = tmp_path / "oversized.txt"
    original = "x" * (1024 * 1024 + 1)
    target.write_text(original, encoding="utf-8")
    first_process = ToolContext(
        cwd=tmp_path,
        max_file_bytes=2 * 1024 * 1024,
        max_read_bytes=2 * 1024 * 1024,
    )
    first_process.configure_snapshot_store(
        "test-agent",
        tmp_path / "sessions" / "oversized.jsonl",
        state_dir=tmp_path / "state",
    )
    fs.read("oversized.txt", show_line_numbers=False, context=first_process)
    patch_tool = build_default_registry().resolve("patch")

    result = call_tool(
        patch_tool,
        {"file_path": "oversized.txt", "old_string": original, "new_string": "changed\n"},
        first_process,
    )
    persisted = sorted(first_process.snapshot_store.glob("*/*.snapshot"))
    assert len(persisted) == 1
    persisted_size = persisted[0].stat().st_size
    second_process = _persistent_context(tmp_path, "oversized")
    restarted_undo = fs.undo("oversized.txt", context=second_process)

    assert result.endswith(
        "WARNING: snapshot exceeds the 1 MiB session undo cap; this undo is available only until the process exits"
    )
    assert persisted_size < 1024 * 1024
    assert restarted_undo.startswith(f"ERROR: discarded unusable snapshot for {target}:")
    assert target.read_text(encoding="utf-8") == "changed\n"
