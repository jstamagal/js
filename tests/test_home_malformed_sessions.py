"""Malformed legacy tool metadata must not prevent filing intact sessions."""

import io
import json

import pytest

from js import home, paths, session_store
from js.session_catalog import first_metadata


@pytest.mark.parametrize("tool_calls", [1, True, "bad", {"function": "bad"}, None])
def test_read_old_session_ignores_nonlist_calls(tmp_path, tool_calls):
    source = tmp_path / "old.jsonl"
    source.write_text(json.dumps({"role": "assistant", "tool_calls": tool_calls}) + "\n", encoding="utf-8")

    session = home._read_old_session(tmp_path, source)

    assert session.tasks == []


@pytest.mark.parametrize("function", ["bad", [], ["bad"], 1, True, None])
def test_migration_preserves_sessions_with_malformed_call_metadata(tmp_path, function):
    folder = paths.legacy_homes()["data"] / "sessions" / "defaultagent"
    folder.mkdir(parents=True)
    parent = folder / "old.jsonl"
    records = [
        {"role": "user", "content": "parent opener", "ts": 100},
        {"role": "assistant", "content": "retained reply", "ts": 101, "tool_calls": [
            None, "bad", 1, {"function": function},
            {"function": {"name": "task", "arguments": json.dumps({"tasks": ["valid neighbor task"]})}},
        ]},
        {"role": "user", "content": "next valid row", "ts": 103},
    ]
    parent.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    child_folder = folder.parent / "reviewer"
    child_folder.mkdir()
    child_records = [{"role": "user", "content": "valid neighbor task", "ts": 102}]
    child = child_folder / "child.jsonl"
    child.write_text(json.dumps(child_records[0]) + "\n", encoding="utf-8")

    parsed = home._read_old_session(folder, parent)
    assert parsed.tasks == [(101, "valid neighbor task")]
    home.migrate_once(io.StringIO())

    filed = session_store.folder_for(paths.user_home()) / parent.name
    filed_child = session_store.subagent_folder(filed) / child.name
    assert paths.home_migration_marker().is_file()
    assert filed.is_file() and filed_child.is_file()
    migrated = [json.loads(line) for line in filed.read_text(encoding="utf-8").splitlines()]
    for original in records:
        assert any(all(record.get(key) == value for key, value in original.items()) for record in migrated)
    assert first_metadata(filed_child)["parent_session"] == str(filed)
    assert not folder.exists()
    assert home.migrate_once(io.StringIO()) == []
