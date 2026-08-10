from __future__ import annotations

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
        max_line_chars=2 * 1024 * 1024,
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
