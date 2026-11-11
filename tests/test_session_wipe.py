from js import memory, session_store


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
