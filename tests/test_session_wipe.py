from types import SimpleNamespace

import pytest

from js import cli, memory, session_store


@pytest.mark.parametrize("append_mark", [False, True])
def test_wipe_allows_first_new_turn_to_save(tmp_path, append_mark):
    session = tmp_path / "active.jsonl"
    old_messages = [{"role": "user", "content": "old turn"}]
    memory.persist_messages(session, old_messages)
    state = {"messages": memory.load_replay_messages(session)}

    cli._cmd_wipe("", state, SimpleNamespace(session_file=session))

    assert state["messages"] == []
    if append_mark:
        memory.append_mark(session, "turn_mode:test")
    new_messages = [
        {"role": "user", "content": "new question"},
        {"role": "assistant", "content": "new answer"},
    ]
    state["messages"].extend(new_messages)
    memory.persist_messages(session, state["messages"])
    assert memory.load_messages(session) == new_messages
    archives = [path for path in tmp_path.glob("*.jsonl") if path != session]
    assert len(archives) == 1
    assert memory.load_messages(archives[0]) == old_messages


def test_wipe_moves_subagent_runs_with_session(tmp_path):
    session = tmp_path / "active.jsonl"
    memory.append_message(session, {"role": "user", "content": "old turn"})
    text = session_store.text_path(session)
    text.write_text("old transcript\n")
    runs = session_store.subagent_folder(session)
    nested_run = runs / "task-1-abcd" / "child.jsonl"
    nested_run.parent.mkdir(parents=True)
    nested_run.write_text("child record\n")
    unrelated = tmp_path / "other" / "child.jsonl"
    unrelated.parent.mkdir()
    unrelated.write_text("unrelated\n")

    moved = memory.wipe(session)

    assert moved is not None
    assert not session.exists()
    assert not text.exists()
    assert not runs.exists()
    assert session_store.text_path(moved).read_text() == "old transcript\n"
    assert (session_store.subagent_folder(moved) / nested_run.relative_to(runs)).read_text() == "child record\n"
    assert unrelated.read_text() == "unrelated\n"
    assert memory.load_messages(moved) == [{"role": "user", "content": "old turn"}]
