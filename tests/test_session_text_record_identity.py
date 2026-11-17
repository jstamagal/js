"""Transcript replay numbering follows the same malformed-record rules as memory."""

import json

import pytest

from js import memory, session_text


@pytest.mark.parametrize("rehydrated", [1, True, "bad", [1], None, False, [], {}, {"role": "user", "content": "files"}])
def test_compaction_synthetic_rows_match_memory_for_rehydration_shape(tmp_path, rehydrated):
    path = tmp_path / "session.jsonl"
    memory.append_message(path, {"role": "user", "content": "old"})
    memory.append_message(path, {"role": "user", "content": "retained"})
    marker = "compaction:" + json.dumps({"summary": "summary", "keep_from": 1, "rehydrated": rehydrated})
    memory.append_mark(path, marker)

    transcript = session_text.parse(path)
    replay = memory.load_replay_messages(path)

    assert len(transcript.replay) == len(replay)
    assert [entry["role"] for entry in transcript.replay] == [entry["role"] for entry in replay]
    memory.append_message(path, {"role": "user", "content": "discarded"})
    memory.append_mark(path, f"rollback_to:{len(replay)}")
    transcript = session_text.parse(path)
    assert len(transcript.replay) == len(memory.load_replay_messages(path))
    assert any("retained" in row.first() for row in transcript.rows())
    assert not any("discarded" in row.first() for row in transcript.rows())


@pytest.mark.parametrize("bad_message", [None, "bad", [], 1, True])
def test_unreadable_message_id_does_not_replace_previous_row_id(tmp_path, bad_message):
    path = tmp_path / "session.jsonl"
    records = [
        {"kind": "message", "version": 1, "id": "first", "ts": 1,
         "message": {"role": "user", "content": "first valid"}},
        {"kind": "message", "version": 1, "id": "bad", "ts": 2, "message": bad_message},
        {"kind": "message", "version": 1, "id": "next", "ts": 3,
         "message": {"role": "assistant", "content": "next valid"}},
    ]
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    original = path.read_bytes()

    transcript = session_text.parse(path)
    replay = memory.load_replay_messages(path)

    assert [row.id for row in transcript.rows()] == ["first", "next"]
    assert transcript.numbers == {"first": 1, "next": 2}
    assert [row.first() for row in transcript.rows()] == ["first valid", "next valid"]
    assert len(transcript.replay) == len(replay) == 2
    assert path.read_bytes() == original


def _record(kind: str, body: dict, version: int | None = 1) -> dict:
    record = {"kind": kind, "ts": 1, **body}
    if version is not None:
        record["version"] = version
    return record


def _message(role: str, text: str, version: int | None = 1) -> dict:
    return _record("message", {"message": {"role": role, "content": text}}, version)


def _mark(marker: str, version: int | None = 1) -> dict:
    return _record("mark", {"marker": marker}, version)


@pytest.mark.parametrize("skipped", [
    _message("narrator", "a role no history holds"),
    _message("user", "a record without a schema version", version=None),
    _message("user", "a record from another schema version", version=999),
])
def test_rollback_hides_the_rows_replay_drops_past_a_record_replay_skips(tmp_path, skipped):
    path = tmp_path / "session.jsonl"
    records = [_message("user", "one"), skipped, _message("assistant", "two"),
               _message("user", "aborted"), _mark("rollback_to:2"), _message("user", "retry")]
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")

    shown = [row.first() for row in session_text.parse(path).rows()]

    assert [m["content"] for m in memory.load_replay_messages(path)] == ["one", "two", "retry"]
    assert "two" in shown and "retry" in shown and "aborted" not in shown


def test_a_mark_replay_skips_hides_no_rows(tmp_path):
    path = tmp_path / "session.jsonl"
    records = [_message("user", "one"), _message("assistant", "two"),
               _mark("rollback_to:0", version=2), _message("user", "three")]
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")

    assert [row.first() for row in session_text.parse(path).rows()] == ["one", "two", "three"]
    assert len(memory.load_replay_messages(path)) == 3
