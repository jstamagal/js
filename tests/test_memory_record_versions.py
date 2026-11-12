"""Only integer-versioned records may alter the replayed conversation."""

import json

import pytest

from js import memory


def write_records(path, records):
    path.write_text("".join(json.dumps(record) + "\n" for record in records))


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

    messages, skipped = memory._replay(session)
    assert messages == [{"role": "user", "content": "keep"}]
    assert skipped == 1
    assert memory.load_messages(session) == messages
    assert capsys.readouterr().err.strip()


def test_integer_current_version_remains_valid(tmp_path, capsys):
    session = tmp_path / "valid.jsonl"
    write_records(session, [message()])
    assert memory.load_messages(session) == [{"role": "user", "content": "keep"}]
    assert not capsys.readouterr().err
