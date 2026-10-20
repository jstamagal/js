"""The read gate on write and patch outlives the process: a read recorded in a
session counts after js restarts on that session, and an overwrite that names
the hash it expects needs no read at all."""
from __future__ import annotations

from js.toolkit import fs
from js.toolkit.core import ToolContext


def _context(tmp_path, session):
    context = ToolContext(cwd=tmp_path)
    context.configure_snapshot_store("js", session, state_dir=tmp_path / "state")
    return context


def test_a_read_before_a_restart_lets_the_resumed_session_overwrite(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("old\n", encoding="utf-8")
    session = tmp_path / "s.jsonl"
    fs.fs_read(file_path=str(target), context=_context(tmp_path, session))

    resumed = _context(tmp_path, session)
    result = fs.write(file_path=str(target), content="new\n", overwrite=True, context=resumed)

    assert result.startswith("wrote "), result
    assert target.read_text(encoding="utf-8") == "new\n"


def test_an_overwrite_naming_the_current_hash_needs_no_read(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("old\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    result = fs.write(file_path=str(target), content="new\n", overwrite=True,
                      expected_hash=fs._hash_bytes(b"old\n"), context=context)

    assert result.startswith("wrote "), result
    assert target.read_text(encoding="utf-8") == "new\n"


def test_an_overwrite_naming_a_stale_hash_is_refused_with_both_hashes(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("moved\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)
    stale = fs._hash_bytes(b"old\n")

    result = fs.write(file_path=str(target), content="new\n", overwrite=True,
                      expected_hash=stale, context=context)

    assert result.startswith("ERROR")
    assert stale in result and fs._hash_bytes(b"moved\n") in result
    assert target.read_text(encoding="utf-8") == "moved\n"


def test_switching_sessions_restores_only_the_selected_sessions_reads(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("old\n", encoding="utf-8")
    first_session = tmp_path / "first.jsonl"
    context = _context(tmp_path, first_session)
    fs.fs_read(str(target), context=context)
    context.configure_snapshot_store("js", tmp_path / "second.jsonl", state_dir=tmp_path / "state")

    refused = fs.write(str(target), content="wrong\n", overwrite=True, context=context)

    assert refused.startswith("ERROR:")
    assert target.read_text(encoding="utf-8") == "old\n"
    context.configure_snapshot_store("js", first_session, state_dir=tmp_path / "state")
    restored = fs.write(str(target), content="new\n", overwrite=True, context=context)
    assert not restored.startswith("ERROR:")
    assert target.read_text(encoding="utf-8") == "new\n"
