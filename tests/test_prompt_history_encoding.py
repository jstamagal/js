import json

import pytest

from js.prompt_history import Entry, Origin, PromptHistory, append_entry, read_entries


@pytest.mark.parametrize("text", ["paste\udcff", "paste\ud800", "雪 🌴\nsecond line", "literal \\udcff"])
def test_history_round_trips_pasted_text_as_valid_utf8_json(tmp_path, text):
    path = tmp_path / "history.jsonl"
    entry = Entry(ts=1, cwd="/tmp", session="s", agent="a", text=text)

    append_entry(path, entry)

    raw = path.read_bytes().decode("utf-8")
    assert len(raw.splitlines()) == 1
    assert json.loads(raw)["text"] == text
    assert read_entries(path) == [entry]


def test_prompt_history_store_string_keeps_surrogate_paste_and_later_prompt(tmp_path):
    path = tmp_path / "history.jsonl"
    history = PromptHistory(path, origin=lambda: Origin(cwd="/tmp", session="s", agent="a"))

    history.store_string("paste\udcff")
    history.store_string("next prompt")

    assert [entry.text for entry in read_entries(path)] == ["paste\udcff", "next prompt"]
