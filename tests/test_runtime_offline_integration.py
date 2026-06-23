from __future__ import annotations

import json

import ai
import ai.types.messages
import ai.types.usage
import pytest

from js import events, runtime, tools as runtime_tools
from js.config import Config
from js.model_client import ModelStreamResult, ModelToolCall
from js.toolkit import ToolContext, build_default_registry


def _make_assistant_msg(
    text: str,
    tool_calls: list[ModelToolCall] | None = None,
    reasoning: str = "",
) -> ai.types.messages.Message:
    """Build an assistant ``Message`` matching the parts the runtime expects."""
    parts: list[ai.types.messages.Part] = []
    if reasoning:
        parts.append(ai.thinking(reasoning))
    if text:
        parts.append(ai.types.messages.TextPart(text=text))
    if tool_calls:
        for tc in tool_calls:
            parts.append(
                ai.types.messages.ToolCallPart(
                    tool_call_id=tc.id,
                    tool_name=tc.name,
                    tool_args=tc.arguments,
                )
            )
    # Parts must be non-empty for valid Message construction.
    if not parts:
        parts.append(ai.types.messages.TextPart(text=""))
    return ai.types.messages.Message(role="assistant", parts=parts)


def model_text_result(text: str, reasoning: str = "") -> ModelStreamResult:
    """Build a ``ModelStreamResult`` that simulates a text-only assistant turn."""
    tool_calls: list[ModelToolCall] = []
    assistant_msg = _make_assistant_msg(text, tool_calls, reasoning)
    return ModelStreamResult(
        text=text,
        tool_calls=tool_calls,
        reasoning=reasoning,
        usage=ai.types.usage.Usage(input_tokens=10, output_tokens=len(text)),
        finish_reason="stop",
        assistant_message=assistant_msg,
    )


def model_tool_call_result(
    name: str,
    args_chunks: list[str],
    call_id: str = "call_test",
    reasoning: str = "",
) -> ModelStreamResult:
    """Build a ``ModelStreamResult`` that simulates a single tool-call assistant turn."""
    args = "".join(args_chunks)
    tool_calls = [ModelToolCall(id=call_id, name=name, arguments=args)]
    assistant_msg = _make_assistant_msg("", tool_calls, reasoning)
    return ModelStreamResult(
        text="",
        tool_calls=tool_calls,
        reasoning=reasoning,
        usage=ai.types.usage.Usage(input_tokens=5, output_tokens=len(args)),
        finish_reason="tool_calls",
        assistant_message=assistant_msg,
    )


def model_parallel_tool_calls_result(
    calls: list[tuple[str, str, str]],
) -> ModelStreamResult:
    """Build a ``ModelStreamResult`` with parallel tool calls.

    Each element is ``(call_id, name, args_json)``.
    """
    tool_calls = [ModelToolCall(id=cid, name=n, arguments=a) for cid, n, a in calls]
    assistant_msg = _make_assistant_msg("", tool_calls)
    return ModelStreamResult(
        text="",
        tool_calls=tool_calls,
        reasoning="",
        usage=ai.types.usage.Usage(input_tokens=5, output_tokens=10),
        finish_reason="tool_calls",
        assistant_message=assistant_msg,
    )


def offline_config(tmp_path, model: str = "offline-test-model", settings: dict | None = None) -> Config:
    return Config(
        agent_id="test-agent",
        agent_dir=tmp_path / ".js" / "sessions" / "test-agent",
        model=model,
        provider_id=None,
        provider_base_url=None,
        provider_api_key=None,
        reasoning_effort=None,
        max_output_tokens=None,
        max_tool_iterations=5,
        max_bash_output_bytes=65536,
        max_tool_result_bytes=65536,
        fetch_timeout_s=5,
        debug_log=None,
        trace=False,
        history_file=tmp_path / ".history",
        sessions_dir=tmp_path / ".js" / "sessions" / "test-agent",
        session_file=tmp_path / ".js" / "sessions" / "test-agent" / "runtime.jsonl",
        prompts_dir=tmp_path / "prompts",
        settings=settings or {},
    )


_CLAUDE_ALIAS_SETTINGS = {
    "tools": {
        "alias_profiles": [
            {"match": ["claude"], "aliases": {"read": "Read", "write": "Write", "task": "Task"}},
        ]
    }
}


class RecordingHooks(events.EventHooks):
    def __init__(self) -> None:
        super().__init__()
        self.emitted: list[tuple[str, dict]] = []

    def emit(self, event: str, **payload):
        self.emitted.append((event, payload))
        return super().emit(event, **payload)


def test_run_turn_emits_text_response_events(monkeypatch, tmp_path):
    hooks = RecordingHooks()

    def stream_stub(**kwargs):
        kwargs["on_text"]("OK")
        return model_text_result("OK")

    monkeypatch.setattr(runtime.model_client, "stream_model", stream_stub)
    cfg = offline_config(tmp_path)
    messages = [{"role": "user", "content": "Say OK."}]

    runtime.run_turn(
        cfg,
        "system",
        messages,
        runtime.Telemetry(None),
        trace_override=False,
        suppress_output=True,
        event_hooks=hooks,
    )

    assert [event for event, _payload in hooks.emitted] == [
        "turn_start",
        "prompt",
        "stream",
        "response",
        "turn_end",
    ]
    assert hooks.emitted[1][1]["message_count"] == 2
    assert hooks.emitted[2][1]["text"] == "OK"
    assert hooks.emitted[3][1]["text"] == "OK"
    assert hooks.emitted[-1][1]["reason"] == "stop"


def test_run_turn_emits_tool_call_and_result_events(monkeypatch, tmp_path):
    target = tmp_path / "note.txt"
    target.write_text("needle\n", encoding="utf-8")
    hooks = RecordingHooks()
    calls: list[dict] = []

    def stream_stub(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return model_tool_call_result("read", [json.dumps({"file_path": "note.txt"})])
        return model_text_result("DONE")

    monkeypatch.setattr(runtime.model_client, "stream_model", stream_stub)
    cfg = offline_config(tmp_path)
    messages = [{"role": "user", "content": "Read note.txt."}]

    runtime.run_turn(
        cfg,
        "system",
        messages,
        runtime.Telemetry(None),
        trace_override=False,
        tool_context=ToolContext(cwd=tmp_path),
        suppress_output=True,
        event_hooks=hooks,
    )

    emitted = [(event, payload) for event, payload in hooks.emitted]
    tool_call = next(payload for event, payload in emitted if event == "tool_call")
    tool_result = next(payload for event, payload in emitted if event == "tool_result")
    assert tool_call["name"] == "read"
    assert tool_call["id"] == "call_test"
    assert tool_result["name"] == "read"
    assert "needle" in tool_result["result"]
    assert emitted[-1][0] == "turn_end"
    assert emitted[-1][1]["reason"] == "stop"


def test_run_turn_emits_turn_end_after_fatal_error(monkeypatch, tmp_path):
    hooks = RecordingHooks()

    def stream_stub(**kwargs):
        raise ValueError("bad request")

    monkeypatch.setattr(runtime.model_client, "stream_model", stream_stub)
    cfg = offline_config(tmp_path)
    messages = [{"role": "user", "content": "Break."}]

    with pytest.raises(ValueError, match="bad request"):
        runtime.run_turn(
            cfg,
            "system",
            messages,
            runtime.Telemetry(None),
            trace_override=False,
            suppress_output=True,
            event_hooks=hooks,
        )

    assert [event for event, _payload in hooks.emitted] == [
        "turn_start",
        "prompt",
        "error",
        "turn_end",
    ]
    assert hooks.emitted[-1][1]["reason"] == "error"


def test_run_turn_applies_config_alias_profile_to_outgoing_tool_specs(monkeypatch, tmp_path):
    calls: list[dict] = []

    def stream_stub(**kwargs):
        calls.append(kwargs)
        return model_text_result("OK")

    monkeypatch.setattr(runtime.model_client, "stream_model", stream_stub)
    cfg = offline_config(tmp_path, model="openai/proxy-claude-sonnet-4", settings=_CLAUDE_ALIAS_SETTINGS)
    messages = [{"role": "user", "content": "Use tools if needed."}]
    registry = build_default_registry().select(["read", "write", "task", "fs_search", "shell"])

    runtime.run_turn(
        cfg,
        "system",
        messages,
        runtime.Telemetry(None),
        trace_override=False,
        tool_registry=registry,
        tool_context=ToolContext(cwd=tmp_path),
        suppress_output=True,
    )

    assert len(calls) == 1
    actual = [spec.name for spec in calls[0]["tools"]]
    expected = ["Read", "Write", "fs_search", "shell", "Task"]
    assert actual == expected
    assert messages[-1] == {"role": "assistant", "content": "OK"}


def test_run_turn_without_alias_profile_keeps_default_tool_names(monkeypatch, tmp_path):
    calls: list[dict] = []

    def stream_stub(**kwargs):
        calls.append(kwargs)
        return model_text_result("OK")

    monkeypatch.setattr(runtime.model_client, "stream_model", stream_stub)
    # Same model id that the old implicit magic would have capitalized; with no
    # configured profile the default lowercase names must be sent verbatim.
    cfg = offline_config(tmp_path, model="openai/proxy-claude-sonnet-4")
    messages = [{"role": "user", "content": "Use tools if needed."}]
    registry = build_default_registry().select(["read", "write", "task", "fs_search", "shell"])

    runtime.run_turn(
        cfg,
        "system",
        messages,
        runtime.Telemetry(None),
        trace_override=False,
        tool_registry=registry,
        tool_context=ToolContext(cwd=tmp_path),
        suppress_output=True,
    )

    actual = [spec.name for spec in calls[0]["tools"]]
    assert actual == ["read", "write", "fs_search", "shell", "task"]


def test_run_turn_streams_fs_search_tool_call_returns_content_before_final_text(monkeypatch, tmp_path):
    target = tmp_path / "notes.txt"
    target.write_text("alpha\nsecret_token=42\nomega\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)
    calls: list[dict] = []

    def stream_stub(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            args = json.dumps(
                {
                    "pattern": "secret_token",
                    "path": ".",
                    "glob": "*.txt",
                    "output_mode": "content",
                }
            )
            return model_tool_call_result("fs_search", [args[:35], args[35:]])
        assert any(
            msg.role == "tool"
            and any(
                getattr(p, "result", None) and "secret_token=42" in str(p.result)
                for p in msg.parts
            )
            for msg in kwargs["messages"]
        )
        return model_text_result("DONE")

    monkeypatch.setattr(runtime.model_client, "stream_model", stream_stub)
    cfg = offline_config(tmp_path)
    messages = [{"role": "user", "content": "Find the secret token."}]

    runtime.run_turn(
        cfg,
        "system",
        messages,
        runtime.Telemetry(None),
        trace_override=False,
        tool_context=context,
        suppress_output=True,
    )

    assert len(calls) == 2
    assert messages[1]["role"] == "assistant"
    assert messages[1]["tool_calls"][0]["function"]["name"] == "fs_search"
    assert messages[2]["role"] == "tool"
    assert messages[2]["name"] == "fs_search"
    assert "notes.txt:2:secret_token=42" in messages[2]["content"]
    assert messages[-1] == {"role": "assistant", "content": "DONE"}


def test_run_turn_hydrates_tool_context_caps_from_config(monkeypatch, tmp_path):
    calls = 0
    context = ToolContext(cwd=tmp_path)

    def stream_stub(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return model_tool_call_result("shell", [json.dumps({"command": "printf abcdef"})])
        assert any(
            msg.role == "tool"
            and any(
                getattr(p, "result", None) and "--- stdout ---\nabc" in str(p.result)
                for p in msg.parts
            )
            for msg in kwargs["messages"]
        )
        return model_text_result("CAPPED")

    monkeypatch.setattr(runtime.model_client, "stream_model", stream_stub)
    cfg = offline_config(tmp_path)
    cfg = Config(**{**cfg.__dict__, "max_bash_output_bytes": 3, "max_tool_result_bytes": 128, "fetch_timeout_s": 9})
    messages = [{"role": "user", "content": "run shell"}]

    runtime.run_turn(
        cfg,
        "system",
        messages,
        runtime.Telemetry(None),
        trace_override=False,
        tool_context=context,
        suppress_output=True,
    )

    assert calls == 2
    assert context.max_bash_output_bytes == 3
    assert context.max_tool_result_bytes == 128
    assert context.fetch_timeout_s == 9
    assert "abcdef" not in messages[2]["content"]
    assert messages[-1] == {"role": "assistant", "content": "CAPPED"}


def test_alias_profile_tool_call_dispatches_to_canonical_and_persists_lowercase(monkeypatch, tmp_path):
    target = tmp_path / "note.txt"
    target.write_text("hello\n", encoding="utf-8")
    calls = 0

    def stream_stub(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            sent_names = [spec.name for spec in kwargs["tools"]]
            assert "Read" in sent_names
            return model_tool_call_result("Read", [json.dumps({"file_path": "note.txt"})])
        return model_text_result("READ_OK")

    monkeypatch.setattr(runtime.model_client, "stream_model", stream_stub)
    cfg = offline_config(tmp_path, model="openai/proxy-claude-sonnet-4", settings=_CLAUDE_ALIAS_SETTINGS)
    registry = build_default_registry().select(["read"])
    messages = [{"role": "user", "content": "read note"}]

    runtime.run_turn(
        cfg,
        "system",
        messages,
        runtime.Telemetry(None),
        trace_override=False,
        tool_registry=registry,
        tool_context=ToolContext(cwd=tmp_path),
        suppress_output=True,
    )

    assert messages[1]["tool_calls"][0]["function"]["name"] == "read"
    assert messages[2]["name"] == "read"
    assert messages[-1] == {"role": "assistant", "content": "READ_OK"}


def test_tool_retry_limit_appends_assistant_error_instead_of_silent_no_response(monkeypatch, tmp_path):
    calls = 0

    def stream_stub(**kwargs):
        nonlocal calls
        calls += 1
        return model_tool_call_result("missing_tool", ["{}"], call_id=f"missing_{calls}")

    monkeypatch.setattr(runtime.model_client, "stream_model", stream_stub)
    cfg = offline_config(tmp_path)
    messages = [{"role": "user", "content": "call missing tool repeatedly"}]

    runtime.run_turn(
        cfg,
        "system",
        messages,
        runtime.Telemetry(None),
        trace_override=False,
        tool_context=ToolContext(cwd=tmp_path),
        suppress_output=True,
    )

    assert calls == 3
    assert messages[-1]["role"] == "assistant"
    assert messages[-1]["content"].startswith("ERROR: tool retry limit reached after missing_tool")
    assert "ERROR: no tool named missing_tool" in messages[-1]["content"]


def test_parallel_failing_calls_all_get_tool_messages_before_retry_limit_failure(monkeypatch, tmp_path):
    """Regression: 3 parallel calls of one tool can saturate the error tracker inside a
    single batch. The old code returned after appending only the FIRST tool message,
    orphaning the other tool_call_ids — DeepSeek/OpenAI then 400 the whole session."""
    calls = 0

    def stream_stub(**kwargs):
        nonlocal calls
        calls += 1
        return model_parallel_tool_calls_result([
            ("orphan_1", "missing_tool", "{}"),
            ("orphan_2", "missing_tool", "{}"),
            ("orphan_3", "missing_tool", "{}"),
        ])

    monkeypatch.setattr(runtime.model_client, "stream_model", stream_stub)
    cfg = offline_config(tmp_path)
    messages = [{"role": "user", "content": "fire three bad calls at once"}]

    runtime.run_turn(
        cfg,
        "system",
        messages,
        runtime.Telemetry(None),
        trace_override=False,
        tool_context=ToolContext(cwd=tmp_path),
        suppress_output=True,
    )

    assert calls == 1
    batch = next(m for m in messages if m.get("tool_calls"))
    call_ids = [c["id"] for c in batch["tool_calls"]]
    assert call_ids == ["orphan_1", "orphan_2", "orphan_3"]
    batch_idx = messages.index(batch)
    between = messages[batch_idx + 1:-1]
    assert [m.get("tool_call_id") for m in between] == call_ids
    assert all(m["role"] == "tool" for m in between)
    assert messages[-1]["role"] == "assistant"
    assert messages[-1]["content"].startswith("ERROR: tool retry limit reached after missing_tool")


def test_run_turn_streams_tool_call_dispatches_real_sem_search_then_final_text(monkeypatch, tmp_path):
    target = tmp_path / "worker.py"
    target.write_text("def task_backend():\n    return 'ready'\n", encoding="utf-8")
    runtime_tools.DEFAULT_CONTEXT = ToolContext(cwd=tmp_path)
    calls: list[dict] = []

    def stream_stub(**kwargs):
        calls.append(kwargs)
        assert any(spec.name == "sem_search" for spec in kwargs["tools"])
        if len(calls) == 1:
            return model_tool_call_result(
                "sem_search",
                [
                    '{"queries":[{"query":"task backend",',
                    '"path":".","glob":"*.py","limit":3}]}',
                ],
            )
        return model_text_result("FOUND")

    monkeypatch.setattr(runtime.model_client, "stream_model", stream_stub)
    cfg = Config(
        agent_id="test-agent",
        agent_dir=tmp_path / ".js" / "sessions" / "test-agent",
        model="offline-test-model",
        provider_id=None,
        provider_base_url=None,
        provider_api_key=None,
        reasoning_effort=None,
        max_output_tokens=None,
        max_tool_iterations=5,
        max_bash_output_bytes=65536,
        max_tool_result_bytes=65536,
        fetch_timeout_s=5,
        debug_log=None,
        trace=False,
        history_file=tmp_path / ".history",
        sessions_dir=tmp_path / ".js" / "sessions" / "test-agent",
        session_file=tmp_path / ".js" / "sessions" / "test-agent" / "runtime.jsonl",
        prompts_dir=tmp_path / "prompts",
    )
    messages = [{"role": "user", "content": "Find task backend."}]

    runtime.run_turn(cfg, "system", messages, runtime.Telemetry(None), trace_override=False)

    assert len(calls) == 2
    assert messages[1]["role"] == "assistant"
    assert messages[1]["tool_calls"][0]["function"]["name"] == "sem_search"
    assert messages[2]["role"] == "tool"
    assert messages[2]["name"] == "sem_search"
    assert "worker.py" in messages[2]["content"]
    assert messages[-1] == {"role": "assistant", "content": "FOUND"}


# ---------------------------------------------------------------------------
# reasoning_content echo rule: attached to outgoing convo ONLY when the same
# assistant message carries tool_calls (DeepSeek token-efficiency fix). JSONL
# always keeps the field (archive value, costs nothing on disk).
# ---------------------------------------------------------------------------


def test_assistant_turn_with_tool_calls_keeps_reasoning_in_next_convo(monkeypatch, tmp_path):
    """DeepSeek requires reasoning_content on the assistant turn that carried
    tool_calls (HTTP 400 without it). It MUST ride on the convo sent next call
    AND on the persisted JSONL line (archive value, costs nothing on disk)."""
    calls: list[dict] = []

    def stream_stub(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            args = json.dumps({"pattern": "x", "path": ".", "glob": "*.txt", "output_mode": "content"})
            return model_tool_call_result("fs_search", [args[:35], args[35:]], reasoning="thinking-1")
        return model_text_result("DONE", reasoning="thinking-2")

    monkeypatch.setattr(runtime.model_client, "stream_model", stream_stub)
    cfg = offline_config(tmp_path)
    target = tmp_path / "x.txt"
    target.write_text("x", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)
    messages = [{"role": "user", "content": "find x"}]

    runtime.run_turn(
        cfg, "system", messages, runtime.Telemetry(None),
        trace_override=False, tool_context=context, suppress_output=True,
    )

    assert len(calls) == 2
    # The in-memory record for the tool-call assistant turn carries reasoning_content.
    tool_call_assistant = next(m for m in messages if m.get("role") == "assistant" and m.get("tool_calls"))
    assert tool_call_assistant.get("reasoning_content") == "thinking-1"
    # Second call's outgoing messages MUST echo that reasoning_content on the
    # prior assistant turn — DeepSeek will 400 otherwise.
    prior_in_second_call = next(
        m for m in calls[1]["messages"]
        if m.role == "assistant" and any(p.kind == "tool_call" for p in m.parts)
    )
    assert prior_in_second_call.reasoning == "thinking-1"
    # And it persists to disk too: simulate the CLI's append and reload.
    from js import memory as _mem
    _mem.append_message(cfg.session_file, tool_call_assistant)
    raw = cfg.session_file.read_text(encoding="utf-8").splitlines()
    persisted = json.loads(raw[-1])
    assert persisted["message"]["reasoning_content"] == "thinking-1"
    assert persisted["message"].get("tool_calls")


def test_assistant_turn_without_tool_calls_strips_reasoning_from_next_convo(monkeypatch, tmp_path):
    """DeepSeek IGNORES AND BILLS reasoning_content on assistant messages without
    tool_calls (~500 wasted prompt tokens per turn). Two checks:
      (1) the on-disk JSONL line for that turn STILL has reasoning_content
          (archive value, costs nothing on disk);
      (2) the convo sent to the provider on the next call does NOT have it
          (achieved by the loader's _strip_orphan_reasoning step — the production
          path is: write to JSONL, reload, then build the next convo)."""
    cfg = offline_config(tmp_path)
    from js import memory as _mem

    # --- Simulate the in-memory -> JSONL -> reload -> next-turn path ---
    # The runtime's "history" form of a tool-free assistant turn ALWAYS carries
    # reasoning_content (it's the JSONL archive copy). Write one such line.
    history_record = {
        "role": "assistant",
        "content": "INTERIM",
        "reasoning_content": "wasted-thoughts",  # what the runtime would persist
    }
    _mem.append_message(cfg.session_file, history_record)

    # Reload — this is the canonical "build the next convo" path in production
    # (see cli.py: messages = M.load_messages(cfg.session_file) at every turn).
    reloaded = _mem.load_messages(cfg.session_file)
    # (1) the JSONL line on disk has reasoning_content
    raw = cfg.session_file.read_text(encoding="utf-8").splitlines()
    on_disk = json.loads(raw[-1])
    assert on_disk["message"]["reasoning_content"] == "wasted-thoughts"
    assert on_disk["message"]["content"] == "INTERIM"
    # (2) the reloaded convo sent to the provider does NOT have it
    assert reloaded[0]["role"] == "assistant"
    assert reloaded[0]["content"] == "INTERIM"
    assert "reasoning_content" not in reloaded[0]

    # End-to-end: feed the reloaded convo into a second run_turn and confirm
    # the outgoing provider call omits reasoning_content on the prior turn.
    calls: list[dict] = []

    def stream_stub(**kwargs):
        calls.append(kwargs)
        return model_text_result("DONE", reasoning="thinking-2")

    monkeypatch.setattr(runtime.model_client, "stream_model", stream_stub)
    messages: list[dict] = list(reloaded)  # fresh list to avoid cross-test pollution

    runtime.run_turn(
        cfg, "system", messages, runtime.Telemetry(None),
        trace_override=False, tool_context=ToolContext(cwd=tmp_path), suppress_output=True,
    )

    assert len(calls) == 1
    prior_in_next_call = next(m for m in calls[0]["messages"] if m.role == "assistant")
    assert not prior_in_next_call.reasoning  # stripped by _strip_orphan_reasoning
    assert any(p.kind == "text" and p.text == "INTERIM" for p in prior_in_next_call.parts)
