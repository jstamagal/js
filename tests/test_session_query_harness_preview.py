"""Picker previews hide harness bodies but retain prose after matching end tags."""

import pytest

from js import memory, session_query


@pytest.mark.parametrize("tag", memory.HARNESS_USER_TAGS)
@pytest.mark.parametrize("content", [
    "<{tag}>\nhidden body\n</{tag}> trailing prose\nfollowing prose",
    "<{tag}>hidden body</{tag}> trailing prose\nfollowing prose",
    "<{tag}>\nhidden body </{tag}> trailing prose\nfollowing prose",
])
def test_preview_preserves_prose_after_matching_close(tag, content):
    user, agent = session_query.preview([{"role": "user", "content": content.format(tag=tag)}])

    assert user == "trailing prose following prose"
    assert not agent


def test_preview_uses_matching_close_and_handles_adjacent_blocks():
    content = (
        "before\n<js-reminder>\nhidden one\n</post-compaction-files>\nstill hidden\n"
        "</js-reminder> <compaction-summary>hidden two</compaction-summary> trailing\nafter"
    )

    user, _ = session_query.preview([{"role": "user", "content": content}])

    assert user == "before trailing after"


@pytest.mark.parametrize("content", [
    "literal <js-reminder> text", "<other-tag>ordinary</other-tag> tail", "</js-reminder> ordinary",
])
def test_preview_keeps_literal_or_unrecognized_tags_outside_harness(content):
    assert session_query.preview([{"role": "user", "content": content}])[0] == content


def test_preview_unclosed_harness_stays_hidden():
    content = "visible\n<js-reminder>\nhidden\n</js-reminder-extra>\nstill hidden"

    assert session_query.preview([{"role": "user", "content": content}])[0] == "visible"


def test_actual_session_replay_preview_keeps_tail_after_harness(tmp_path):
    path = tmp_path / "session.jsonl"
    memory.append_message(path, {"role": "user", "content": "<js-reminder>hidden</js-reminder> actual question"})
    memory.append_message(path, {"role": "assistant", "content": "answer"})

    assert session_query.preview(memory.load_replay_messages(path)) == ("actual question", "answer")
