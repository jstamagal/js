"""Command summaries are bounded without altering session journal content."""

import copy
import json

import pytest

from js import memory, session_store, session_text


@pytest.mark.parametrize("length", [10, session_text._ARG_WIDTH - 2, session_text._ARG_WIDTH - 1, 1000])
@pytest.mark.parametrize("encoded", [False, True])
def test_shell_command_summary_uses_same_width_cap(length, encoded):
    arguments = {"command": "x" * length + "\nsecond line"}
    before = copy.deepcopy(arguments)

    result = session_text._call_line(json.dumps(arguments) if encoded else arguments)

    expected = "$ " + "x" * length
    if len(expected) > session_text._ARG_WIDTH:
        expected = expected[:session_text._ARG_WIDTH - 1] + "…"
    assert result == expected
    assert len(result) <= session_text._ARG_WIDTH
    assert "second line" not in result
    assert arguments == before


def test_command_summary_cap_preserves_full_journal_and_tool_name(tmp_path):
    path = session_store.reserve(session_store.folder_for(tmp_path))
    command = "x" * 1000 + "\nsecond line"
    message = {"role": "assistant", "content": "run command", "tool_calls": [{
        "id": "cmd1", "function": {"name": "shell", "arguments": json.dumps({"command": command})},
    }]}
    memory.append_message(path, message)
    memory.append_message(path, {"role": "tool", "tool_call_id": "cmd1", "content": "done"})
    journal = path.read_bytes()

    transcript = session_text.parse(path)
    rendered = transcript.text()
    indexed = "\n".join(line[1] for line in transcript.search_lines())

    assert "tool:shell" in rendered
    assert "$ " + "x" * 197 + "…" in rendered
    assert "x" * 1000 not in rendered
    assert "x" * 1000 not in indexed
    assert path.read_bytes() == journal
    stored = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    saved = next(record["message"] for record in stored if record.get("kind") == "message")
    assert json.loads(saved["tool_calls"][0]["function"]["arguments"])["command"] == command
