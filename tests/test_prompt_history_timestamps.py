"""A malformed timestamp cannot reorder the shared prompt history."""

import json

import pytest

from js.prompt_history import _entry, browse_order, read_entries


@pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity", "1e999", "-1e999"])
def test_nonfinite_history_row_is_skipped_before_sorting(tmp_path, raw):
    path = tmp_path / "history.jsonl"
    path.write_text(
        '{"ts":3,"text":"c","cwd":"/here"}\n'
        '{"ts":' + raw + ',"text":"invalid","cwd":"/here"}\n'
        '{"ts":1,"text":"a","cwd":"/elsewhere"}\n'
        '{"ts":2,"text":"b","cwd":"/elsewhere"}\n', encoding="utf-8",
    )

    entries = read_entries(path)

    assert [entry.ts for entry in entries] == [1, 2, 3]
    assert browse_order(entries, "/here", cwd_first=False) == ["c", "b", "a"]
    assert browse_order(entries, "/elsewhere", cwd_first=True) == ["b", "a", "c"]
    assert [entry.text for entry in read_entries(path, limit=2)] == ["b", "c"]
    assert _entry(json.loads('{"ts":' + raw + ',"text":"invalid"}')) is None


@pytest.mark.parametrize("timestamp", [-2.5, 0, 1, 2.5, 1e100])
def test_finite_history_timestamp_is_preserved(timestamp):
    entry = _entry({"ts": timestamp, "text": "valid"})

    assert entry is not None
    assert entry.ts == timestamp


@pytest.mark.parametrize("record", [{"text": "missing"}, {"ts": "2", "text": "string"}, {"ts": None, "text": "null"}])
def test_existing_missing_or_nonnumeric_timestamp_policy_is_preserved(record):
    assert _entry(record).ts == 0
