from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from js import memory, settings
from js.config import from_env, resolve_session_file


def test_memory_loader_ignores_noise_and_honors_reset_and_rollback_marks(tmp_path):
    session = tmp_path / "session.jsonl"
    records = [
        {"kind": "message", "version": 1, "ts": 1, "message": {"role": "user", "content": "before reset"}},
        {"kind": "mark", "version": 1, "ts": 2, "marker": "session_reset"},
        {"kind": "message", "version": 1, "ts": 3, "message": {"role": "user", "content": "kept"}},
        {"kind": "message", "version": 99, "ts": 4, "message": {"role": "assistant", "content": "old schema"}},
        "{not json",
        {"kind": "message", "version": 1, "ts": 5, "message": {"role": "developer", "content": "bad role"}},
        {"kind": "message", "version": 1, "ts": 6, "message": {"role": "assistant", "content": "rolled back"}},
        {"kind": "mark", "version": 1, "ts": 7, "marker": "rollback_to:1"},
        {"kind": "message", "version": 1, "ts": 8, "message": {"role": "assistant", "content": "after rollback"}},
    ]
    session.write_text(
        "\n".join(json.dumps(record) if isinstance(record, dict) else record for record in records) + "\n",
        encoding="utf-8",
    )

    actual = memory.load_messages(session)

    expected = [
        {"role": "user", "content": "kept"},
        {"role": "assistant", "content": "after rollback"},
    ]
    assert actual == expected


def test_memory_loader_heals_orphaned_tool_calls(tmp_path):
    """Sessions persisted by the pre-fix runtime can carry assistant tool_calls with
    missing tool results; the loader backfills synthetic results so providers that
    enforce tool_call/tool pairing (DeepSeek) accept the replayed history."""
    session = tmp_path / "session.jsonl"
    batch = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": "call_a", "type": "function", "function": {"name": "fs_search", "arguments": "{}"}},
            {"id": "call_b", "type": "function", "function": {"name": "fs_search", "arguments": "{}"}},
            {"id": "call_c", "type": "function", "function": {"name": "fs_search", "arguments": "{}"}},
        ],
    }
    records = [
        {"kind": "message", "version": 1, "ts": 1, "message": {"role": "user", "content": "go"}},
        {"kind": "message", "version": 1, "ts": 2, "message": batch},
        {"kind": "message", "version": 1, "ts": 3,
         "message": {"role": "tool", "tool_call_id": "call_a", "name": "fs_search", "content": "ERROR: nope"}},
        {"kind": "message", "version": 1, "ts": 4,
         "message": {"role": "assistant", "content": "ERROR: tool retry limit reached after fs_search"}},
    ]
    session.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

    actual = memory.load_messages(session)

    tool_msgs = [m for m in actual if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["call_a", "call_b", "call_c"]
    assert tool_msgs[0]["content"] == "ERROR: nope"
    for synthetic in tool_msgs[1:]:
        assert synthetic["name"] == "fs_search"
        assert synthetic["content"] == "ERROR: tool result was not recorded (session interrupted)"
    # synthetics sit between the real result and the failure message
    assert actual[-1] == {"role": "assistant", "content": "ERROR: tool retry limit reached after fs_search"}
    assert actual[0] == {"role": "user", "content": "go"}


def test_memory_append_mark_append_message_and_wipe_rotate_session(tmp_path):
    session = tmp_path / "agent" / "sessions" / "active.jsonl"

    memory.append_mark(session, "session_start")
    memory.append_message(session, {"role": "user", "content": "hello"})
    memory.append_message(session, {"role": "assistant", "content": "world"})
    actual_messages = memory.load_messages(session)
    actual_backup = memory.wipe(session)

    expected_messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "world"},
    ]
    assert actual_messages == expected_messages
    assert actual_backup == session.with_suffix(".jsonl.bak")
    assert not session.exists()
    assert actual_backup.exists()
    assert memory.load_messages(actual_backup) == expected_messages


def test_from_env_jsrc_boolean_numeric_settings_fall_back_to_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_SESSION", raising=False)
    for name in (
        "JS_MAX_OUTPUT_TOKENS",
        "JS_MAX_TOOL_ITERATIONS",
        "JS_MAX_BASH_OUTPUT_BYTES",
        "JS_MAX_TOOL_RESULT_BYTES",
        "JS_FETCH_TIMEOUT",
    ):
        monkeypatch.delenv(name, raising=False)
    config_dir = tmp_path / ".config" / "js"
    config_dir.mkdir(parents=True)
    (config_dir / "jsrc").write_text(
        """set model.max_output_tokens true
set limits.max_tool_iterations true
set limits.max_bash_output_bytes true
set limits.max_tool_result_bytes true
set limits.fetch_timeout_s true
set limits.max_read_lines true
set limits.max_line_chars true
set limits.max_file_bytes true
set limits.task_max_depth true
set limits.wiki_vault_lock_timeout_s true
""",
        encoding="utf-8",
    )

    actual = from_env(save_session=False)

    assert actual.max_output_tokens is None
    assert actual.max_tool_iterations == settings.DEFAULT_MAX_TOOL_ITERATIONS
    assert actual.max_bash_output_bytes == settings.DEFAULT_MAX_BASH_OUTPUT_BYTES
    assert actual.max_tool_result_bytes == settings.DEFAULT_MAX_TOOL_RESULT_BYTES
    assert actual.fetch_timeout_s == settings.DEFAULT_FETCH_TIMEOUT_S
    assert actual.max_read_lines == settings.DEFAULT_MAX_READ_LINES
    assert actual.max_line_chars == settings.DEFAULT_MAX_LINE_CHARS
    assert actual.max_file_bytes == settings.DEFAULT_MAX_FILE_BYTES
    assert actual.task_max_depth == settings.DEFAULT_TASK_MAX_DEPTH
    assert actual.wiki_vault_lock_timeout_s == settings.DEFAULT_WIKI_VAULT_LOCK_TIMEOUT_S

def test_from_env_respects_provider_runtime_caps_agent_and_no_save(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("JS_PROVIDER", "openai")
    monkeypatch.setenv("JS_MODEL", "custom-model")
    monkeypatch.setenv("JS_BASE_URL", "http://proxy.test/v1")
    monkeypatch.setenv("JS_API_KEY", "sk-proxy")
    monkeypatch.setenv("JS_AGENT", "agent_one")
    monkeypatch.setenv("JS_TRACE", "0")
    monkeypatch.setenv("JS_REASONING", "max")
    monkeypatch.setenv("JS_MAX_OUTPUT_TOKENS", "1234")
    monkeypatch.setenv("JS_MAX_TOOL_ITERATIONS", "7")
    monkeypatch.setenv("JS_MAX_BASH_OUTPUT_BYTES", "4567")
    monkeypatch.setenv("JS_MAX_TOOL_RESULT_BYTES", "8910")
    monkeypatch.setenv("JS_FETCH_TIMEOUT", "42")
    monkeypatch.setenv("JS_VISION", "0")
    monkeypatch.delenv("JS_SESSION", raising=False)

    actual = from_env(save_session=False)

    expected_agent_dir = tmp_path / ".local" / "share" / "js" / "sessions" / "agent_one"
    expected_state_dir = tmp_path / ".local" / "share" / "js" / "state" / "agent_one"
    assert actual.agent_id == "agent_one"
    assert actual.agent_dir == expected_agent_dir
    assert actual.session_file == Path(os.devnull)
    assert actual.history_file == expected_agent_dir / ".history"
    assert actual.prompts_dir.name == "agent_one"
    assert actual.model == "custom-model"
    assert actual.provider_id == "openai"
    assert actual.provider_base_url == "http://proxy.test/v1"
    assert actual.provider_api_key == "sk-proxy"
    # Official OPENAI_* env vars are NOT copied into Config.
    assert not hasattr(actual, "OPENAI_API_BASE")
    assert not hasattr(actual, "OPENAI_API_KEY")
    assert actual.trace is False
    assert actual.reasoning_effort == "high"
    assert actual.max_output_tokens == 1234
    assert actual.max_tool_iterations == 7
    assert actual.max_bash_output_bytes == 4567
    assert actual.max_tool_result_bytes == 8910
    assert actual.fetch_timeout_s == 42
    assert actual.vision_enabled is False
    # save_session=False means no session file, no latest.json.
    assert not (expected_agent_dir / "latest.json").exists()
    # debug.log path now lives under state/, even when JS_DEBUG is unset.
    assert actual.debug_log is None or actual.debug_log == expected_state_dir / "debug.log"


def test_documented_bytes_env_names_are_canonical(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_SESSION", raising=False)
    monkeypatch.setenv("JS_MAX_BASH_OUTPUT_BYTES", "222")
    monkeypatch.setenv("JS_MAX_TOOL_RESULT_BYTES", "444")

    actual = from_env(save_session=False)

    assert actual.max_bash_output_bytes == 222
    assert actual.max_tool_result_bytes == 444


def test_resolve_session_file_rejects_relative_traversal_even_when_target_exists(tmp_path):
    sessions_dir = tmp_path / "agent" / "sessions"
    outside = tmp_path / "outside.jsonl"
    sessions_dir.mkdir(parents=True)
    outside.write_text("", encoding="utf-8")

    with pytest.raises(ValueError, match="inside"):
        resolve_session_file(sessions_dir, "../../outside.jsonl")


def test_load_messages_strips_orphan_reasoning_without_rewriting_file(tmp_path):
    """Pre-fix sessions (or any provider that bill-then-discard reasoning) can
    carry ``reasoning_content`` on a tool-free assistant turn. The loader MUST
    strip it from the rebuilt in-memory conversation (DeepSeek token-efficiency
    fix: the field is wasted on a tool-free turn) while leaving the JSONL bytes
    on disk untouched (archive value)."""
    session = tmp_path / "session.jsonl"
    pre_fix_records = [
        {"kind": "message", "version": 1, "ts": 1,
         "message": {"role": "user", "content": "go"}},
        {"kind": "message", "version": 1, "ts": 2,
         "message": {
             "role": "assistant",
             "content": "no tools used",
             "reasoning_content": "wasted-deepseek-thoughts-that-cost-tokens",
         }},
        {"kind": "message", "version": 1, "ts": 3,
         "message": {
             "role": "assistant",
             "content": "",
             "tool_calls": [
                 {"id": "call_x", "type": "function",
                  "function": {"name": "fs_search", "arguments": "{}"}},
             ],
             "reasoning_content": "needed-because-tool-call",
         }},
        {"kind": "message", "version": 1, "ts": 4,
         "message": {"role": "tool", "tool_call_id": "call_x", "name": "fs_search", "content": "ok"}},
    ]
    raw_bytes = "\n".join(json.dumps(r) for r in pre_fix_records) + "\n"
    session.write_text(raw_bytes, encoding="utf-8")

    rebuilt = memory.load_messages(session)

    # The tool-free assistant turn loses its reasoning_content in the rebuilt convo.
    assert rebuilt[1] == {"role": "assistant", "content": "no tools used"}
    # The tool-call assistant turn keeps it (DeepSeek requires it there).
    assert rebuilt[2]["role"] == "assistant"
    assert rebuilt[2].get("reasoning_content") == "needed-because-tool-call"
    assert rebuilt[2].get("tool_calls")
    # The on-disk bytes are untouched — the archive copy of the tool-free
    # assistant turn still has the wasted reasoning_content.
    assert session.read_text(encoding="utf-8") == raw_bytes
    assert "wasted-deepseek-thoughts-that-cost-tokens" in session.read_text(encoding="utf-8")
