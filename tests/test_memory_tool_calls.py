"""Malformed foreign tool-call metadata does not abort history replay."""

import json

import pytest

from js import memory


@pytest.mark.parametrize("function", ["shell", ["shell"], 5, True])
def test_malformed_function_does_not_break_result_balancing_or_load(tmp_path, function):
    messages = [
        {"role": "assistant", "content": "calls", "tool_calls": [
            {"id": "bad", "function": function},
            {"id": "good", "function": {"name": "read", "arguments": "{}"}},
        ]},
        {"role": "user", "content": "later prompt"},
    ]
    balanced = memory.balance_orphaned_tool_calls(messages)
    assert balanced[0] == messages[0]
    assert balanced[-1] == messages[-1]
    assert [(result["tool_call_id"], result["name"]) for result in balanced[1:-1]] == [
        ("bad", ""), ("good", "read")]

    path = tmp_path / "session.jsonl"
    raw = "\n".join(json.dumps({"kind": "message", "version": 1, "ts": 1, "message": message})
                    for message in messages) + "\n"
    path.write_text(raw, encoding="utf-8")
    assert memory.load_replay_messages(path) == balanced
    assert path.read_text(encoding="utf-8") == raw
