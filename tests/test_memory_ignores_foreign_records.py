"""One conversation file carries records from several writers, each versioning
itself. The message schema judges only its own."""

from __future__ import annotations

import json

from js.memory import SCHEMA_VERSION, append_message, load_messages
from js.session_catalog import record_session_start


def test_session_metadata_does_not_read_as_an_incompatible_message(tmp_path, capsys):
    session = tmp_path / "session.jsonl"
    record_session_start(session, cwd=tmp_path, agent="defaultagent", model="m")
    append_message(session, {"role": "user", "content": "hi"})

    messages = load_messages(session)

    assert [m["role"] for m in messages] == ["user"]
    assert capsys.readouterr().err == ""


def test_a_genuinely_stale_message_is_still_reported(tmp_path, capsys):
    session = tmp_path / "session.jsonl"
    session.write_text(
        json.dumps(
            {
                "kind": "message",
                "ts": 0,
                "version": SCHEMA_VERSION + 1,
                "message": {"role": "user", "content": "from the future"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert load_messages(session) == []
    assert "skipped 1 record" in capsys.readouterr().err


def test_an_unknown_writer_is_ignored_without_a_warning(tmp_path, capsys):
    session = tmp_path / "session.jsonl"
    session.write_text(
        json.dumps({"kind": "some_future_sidecar", "version": 7}) + "\n",
        encoding="utf-8",
    )

    assert load_messages(session) == []
    assert capsys.readouterr().err == ""
