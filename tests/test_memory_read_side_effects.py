"""Missing session reads do not create directories, unlike file creation."""

import pytest

from js import memory


@pytest.mark.parametrize("reader, expected", [
    (memory.load_replay_messages, []),
    (memory.load_messages, []),
    (lambda path: memory._last_mark_payload(path, "checkpoint:"), None),
    (memory.last_reply_stamp, None),
])
def test_missing_session_read_leaves_parent_absent(tmp_path, reader, expected):
    parent = tmp_path / "missing" / "session"
    assert reader(parent / "conversation.jsonl") == expected
    assert not (tmp_path / "missing").exists()


@pytest.mark.parametrize("mode", ["r", "rb", "r+"])
def test_read_open_mode_does_not_create_parent(tmp_path, mode):
    parent = tmp_path / "missing"
    with pytest.raises(FileNotFoundError):
        memory._open_locked(parent / "conversation.jsonl", mode)
    assert not parent.exists()


@pytest.mark.parametrize("mode", ["w", "a", "x", "wb"])
def test_creating_open_mode_retains_parent_creation(tmp_path, mode):
    path = tmp_path / "created" / "conversation.jsonl"
    with memory._open_locked(path, mode):
        pass
    assert path.is_file()


def test_existing_session_reads_and_append_creation_still_work(tmp_path):
    path = tmp_path / "created" / "conversation.jsonl"
    memory.append_message(path, {"role": "user", "content": "question"})
    stamp = memory.stamp_for("model", "provider", "high")
    memory.append_message(path, {"role": "assistant", "content": "answer"}, stamp)
    before = path.read_bytes()
    assert [message["content"] for message in memory.load_replay_messages(path)] == ["question", "answer"]
    assert memory.last_reply_stamp(path) == stamp
    assert memory._last_mark_payload(path, "not-present:") is None
    assert path.read_bytes() == before
