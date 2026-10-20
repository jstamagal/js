"""One bad record must not poison a session's `.txt` (js.session_text):
ALL-CLAIMS.md:523-526. Every test runs in the tmp HOME the conftest installs."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from js import memory, session_store, session_text


def _session(tmp_path: Path) -> Path:
    cwd = tmp_path / "proj"
    cwd.mkdir(exist_ok=True)
    path = session_store.reserve(session_store.folder_for(cwd))
    memory.append_message(path, {"role": "user", "content": "hi"})
    return path


def _staging(path: Path) -> list[str]:
    txt = session_store.text_path(path)
    return sorted(p.name for p in path.parent.glob(f".{txt.name}.*.tmp"))


def test_a_lone_surrogate_is_written_and_later_appends_keep_working(tmp_path):
    path = _session(tmp_path)
    memory.append_message(path, {"role": "assistant", "content": "x\udcff"})
    memory.append_message(path, {"role": "user", "content": "again"})

    text = session_store.text_path(path).read_text(encoding="utf-8")
    assert "x?" in text and "again" in text
    assert _staging(path) == []


def test_a_failed_write_leaves_no_staging_file(tmp_path, monkeypatch):
    target = tmp_path / "out.txt"
    monkeypatch.setattr(session_text.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("full")))
    session_text._write(target, "body")
    assert list(tmp_path.glob(".out.txt.*.tmp")) == []
    assert not target.exists()


@pytest.mark.parametrize("ts", [1.7e12, 1e20, float("nan"), float("inf"), True, "noon"])
def test_a_timestamp_no_clock_can_show_renders_as_a_placeholder(ts):
    transcript = session_text._Transcript()
    transcript.feed({"kind": "session_metadata", "ts": ts})
    transcript.feed({"kind": "message", "ts": ts, "message": {"role": "user", "content": "x"}})
    text = transcript.text()
    assert "--:--" in text and "started: -" in text


@pytest.mark.parametrize("keep_from", [None, "abc", 1.5, True])
def test_a_compaction_mark_with_a_bad_keep_from_is_inert(keep_from):
    transcript = session_text._Transcript()
    for n in range(3):
        transcript.feed({"kind": "message", "ts": 0, "message": {"role": "user", "content": f"m{n}"}})
    before = list(transcript.replay)
    marker = "compaction:" + json.dumps({"summary": "s", "keep_from": keep_from})
    transcript.feed({"kind": "mark", "ts": 0, "marker": marker})
    assert transcript.replay == before
    transcript.text()


def test_a_valid_compaction_mark_still_cuts():
    transcript = session_text._Transcript()
    for n in range(3):
        transcript.feed({"kind": "message", "ts": 0, "message": {"role": "user", "content": f"m{n}"}})
    transcript.feed({"kind": "mark", "ts": 0, "marker": 'compaction:{"summary":"s","keep_from":2}'})
    assert len(transcript.replay) == 2  # summary + the one kept


def test_a_raw_line_separator_inside_a_record_does_not_split_it(tmp_path):
    # An ensure_ascii=False writer leaves U+2028 raw; refresh() fed
    # splitlines() halves and lost the message from the .txt.
    path = _session(tmp_path)
    record = {"kind": "message", "id": "r2", "parent": None, "ts": 0,
              "message": {"role": "assistant", "content": "a\u2028b"}}
    with open(path, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    session_text.refresh(path)
    assert "a\u2028b" in session_store.text_path(path).read_text(encoding="utf-8")


def test_registered_setting_can_stop_automatic_text_refresh(tmp_path):
    from js import settings

    path = _session(tmp_path)
    text_path = session_store.text_path(path)
    before = text_path.read_bytes()
    settings.bind_live_settings({"SESSION_TEXT_AUTO_REFRESH": False})
    memory.append_message(path, {"role": "assistant", "content": "new reply"})
    assert text_path.read_bytes() == before
    assert "new reply" in path.read_text()
