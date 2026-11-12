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
