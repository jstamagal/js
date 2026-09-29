"""The turn's context budget: the conversation the model is sent, and how a
turn brings it under the window before and after the provider refuses it."""

from __future__ import annotations

import asyncio
import io
from dataclasses import replace

import ai
import pytest

from js import compaction, model_client, runtime
from js.toolkit import ToolContext
from js.toolkit.registry import build_default_registry
from js.turn_budget import TurnConvo
from test_lazy_tool_discovery import _cfg, _result


@pytest.fixture(autouse=True)
def offline_metadata(monkeypatch):
    monkeypatch.setattr(runtime, "_resolve_context_window", lambda *a, **k: 1_000_000)
    monkeypatch.setattr(runtime.model_metadata, "resolve_max_output", lambda *a, **k: 4096)


def _overflow():
    return ai.ProviderAPIError(
        "request (97720 tokens) exceeds the available context size (89600 tokens)",
        provider="openai", error_type="exceed_context_size_error",
    )


def _tool_history(n: int) -> list[dict]:
    messages: list[dict] = []
    for index in range(n):
        messages.extend([
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": f"c{index}", "type": "function", "function": {"name": "read", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": f"c{index}", "name": "read", "content": "x" * 1000},
        ])
    messages.append({"role": "user", "content": "continue"})
    return messages


def test_overflow_cleared_resends_the_rebuilt_conversation_from_the_top(monkeypatch, tmp_path):
    cfg = replace(_cfg(tmp_path), provider_id="openai", provider_api_key="offline",
                  provider_base_url="http://127.0.0.1:1/v1")
    calls = []

    def stream(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise _overflow()
        return _result(text="ok")

    monkeypatch.setattr(model_client, "stream_model_async", stream)
    messages = _tool_history(22)
    asyncio.run(runtime.run_turn_async(
        cfg, "SYSTEM", messages, runtime.Telemetry(None, trace_sink=io.StringIO()),
        tool_registry=build_default_registry().select([]),
        tool_context=ToolContext(cwd=tmp_path), suppress_output=True,
    ))
    assert len(calls) == 2
    assert compaction.MICROCOMPACT_CLEARED_MESSAGE not in str(calls[0]["messages"])
    assert compaction.MICROCOMPACT_CLEARED_MESSAGE in str(calls[1]["messages"])
    assert calls[1]["trace_request_from"] == 0
    assert calls[1]["trace_request_schemas"] is True
    assert messages[-1]["content"] == "ok"


# --------------------------------------------------------------------------
# TurnConvo
# --------------------------------------------------------------------------

def _convo(messages):
    return TurnConvo("SYSTEM", messages, provider_id=None, model="offline-test")


def test_the_convo_is_the_sdk_form_of_the_history():
    convo = _convo([{"role": "user", "content": "hi"}])
    assert [m.role for m in convo.ai] == ["system", "user"]
    assert (convo.sent, convo.schemas) == (0, True)


def test_a_rebuild_replaces_the_list_and_restarts_the_trace():
    messages = [{"role": "user", "content": "hi"}]
    convo = _convo(messages)
    before = convo.ai
    convo.traced()
    assert (convo.sent, convo.schemas) == (2, False)
    messages.append({"role": "assistant", "content": "hello"})
    convo.rebuild()
    assert [m.role for m in before] == ["system", "user"]
    assert [m.role for m in convo.ai] == ["system", "user", "assistant"]
    assert (convo.sent, convo.schemas) == (0, True)


def test_added_records_extend_the_convo_without_a_system_message():
    messages = [{"role": "user", "content": "hi"}]
    convo = _convo(messages)
    convo.traced()
    nudge = {"role": "user", "content": "go on"}
    messages.append(nudge)
    convo.add([nudge])
    assert [m.role for m in convo.ai] == ["system", "user", "user"]
    assert (convo.sent, convo.schemas) == (2, False)
