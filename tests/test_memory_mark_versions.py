"""Mark readers use the same schema-version boundary as conversation replay."""

import json

import pytest

from js import memory


@pytest.mark.parametrize("version", [2, True, 1.0, None, "1", "missing"])
def test_foreign_marks_do_not_override_supported_session_state(tmp_path, version):
    path = tmp_path / "session.jsonl"
    records = []

    def mark(marker, current):
        record = {"kind": "mark", "ts": 1, "version": current, "marker": marker}
        if current == "missing":
            del record["version"]
        records.append(record)

    mark("turn_mode:foreign-first", version)
    mark("turn_mode:valid-first", memory.SCHEMA_VERSION)
    mark("system:" + json.dumps({"system": "valid system"}), memory.SCHEMA_VERSION)
    mark("workspace:" + json.dumps({"cwd": "valid directory"}), memory.SCHEMA_VERSION)
    mark("prompt_seen:valid-fingerprint", memory.SCHEMA_VERSION)
    mark("turn_mode:valid-last", memory.SCHEMA_VERSION)
    mark("turn_mode:foreign-last", version)
    mark("system:" + json.dumps({"system": "foreign system"}), version)
    mark("workspace:" + json.dumps({"cwd": "foreign directory"}), version)
    mark("prompt_seen:foreign-fingerprint", version)
    mark("tool_surface:" + json.dumps({"version": 1, "ids": ["native:read"]}), memory.SCHEMA_VERSION)
    mark("session_reset", version)
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    before = path.read_bytes()

    assert memory.first_turn_mode(path) == "valid-first"
    assert memory._last_mark_payload(path, "turn_mode:") == "valid-last"
    assert memory.load_system_prompt(path) == "valid system"
    assert memory.last_workspace(path) == {"cwd": "valid directory"}
    assert memory.last_prompt_seen(path) == "valid-fingerprint"
    assert memory.load_tool_surface(path) == {"version": 1, "ids": ["native:read"]}
    assert path.read_bytes() == before


def test_mark_readers_leave_missing_path_absent(tmp_path):
    path = tmp_path / "missing" / "session.jsonl"
    assert memory.first_turn_mode(path) is None
    assert memory.load_system_prompt(path) is None
    assert memory.last_workspace(path) is None
    assert memory.last_prompt_seen(path) is None
    assert not path.parent.exists()
