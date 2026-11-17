"""Only integer-versioned records may alter the replayed conversation."""

import json

import pytest

from js import memory, messages as msgs


def write_records(path, records):
    path.write_text("".join(json.dumps(record) + "\n" for record in records))


def skipped_notice(session, count):
    """The notice that replaying `session` skipped `count` records."""
    records = f"{count} record{'' if count == 1 else 's'}"
    return msgs.SESSION_RECORDS_SKIPPED.text(path=session, records=records, version=memory.SCHEMA_VERSION)


def message(version=memory.SCHEMA_VERSION):
    return {"kind": "message", "ts": 1, "version": version,
            "message": {"role": "user", "content": "keep"}}


@pytest.mark.parametrize("version", [True, 1.0, False, "1", None, [], {}])
@pytest.mark.parametrize("kind", ["message", "mark"])
def test_noninteger_version_cannot_load_or_reset_messages(tmp_path, capsys, version, kind):
    session = tmp_path / "versions.jsonl"
    invalid = message(version) if kind == "message" else {
        "kind": "mark", "ts": 2, "version": version, "marker": "session_reset"}
    write_records(session, [message(), invalid])

    assert memory.load_messages(session) == [{"role": "user", "content": "keep"}]
    assert skipped_notice(session, 1) in capsys.readouterr().err


def test_integer_current_version_remains_valid(tmp_path, capsys):
    session = tmp_path / "valid.jsonl"
    write_records(session, [message()])
    assert memory.load_messages(session) == [{"role": "user", "content": "keep"}]
    assert not capsys.readouterr().err


def test_missing_owned_versions_are_counted_without_counting_foreign_records(tmp_path, capsys):
    session = tmp_path / "missing.jsonl"
    missing = message()
    del missing["version"]
    write_records(session, [
        message(), missing, {"kind": "mark", "marker": "session_reset", "ts": 2},
        {"kind": "session_metadata", "agent": "worker"},
        {"kind": "future_sidecar", "version": True},
        {"role": "assistant", "content": "legacy bare message"},
    ])
    assert memory.load_messages(session) == [{"role": "user", "content": "keep"}]
    assert skipped_notice(session, 2) in capsys.readouterr().err
