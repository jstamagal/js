"""The reasoning effort dial and the chat-completions reasoning-replay strip.

Supported-effort sets and the glm replay rejection are ground-truthed by live
probe (2026-06-30); these tests pin the resulting js behavior so a refactor
can't silently regress mimo's 400 or the resume/model-switch fix.
"""

from __future__ import annotations

from typing import Any

import ai
import pytest

from js import model_client, reasoning
from js.model_client import ModelStreamResult


# ---- the dial (pure) ----

@pytest.mark.parametrize(
    "model,inp,want",
    [
        ("mimo-v2.5", "xhigh", "high"),      # strict {low,med,high} ceiling
        ("mimo-v2.5", "max", "high"),
        ("mimo-v2.5", "minimal", "low"),     # no minimal stop -> floor
        ("mimo-v2.5", "none", "low"),
        ("kimi-k2.7-code", "max", "high"),
        ("kimi-k2.7-code", "none", "minimal"),
        ("kimi-k2.7-code", "minimal", "minimal"),
        ("glm-5.2", "minimal", "none"),      # tie none/low -> gentler stop
        ("glm-5.2", "max", "max"),
        ("glm-5.2", "none", "none"),
        ("deepseek-v4-pro", "minimal", "low"),
        ("deepseek-v4-pro", "max", "max"),
    ],
)
def test_snap_effort_to_model_stops(model, inp, want):
    assert reasoning.snap_effort(inp, reasoning.supported_efforts(model)) == want


def test_unknown_family_is_passthrough():
    assert reasoning.supported_efforts("some-random-model") is None
    assert reasoning.snap_effort("xhigh", None) == "xhigh"


def test_rejects_reasoning_replay_only_glm():
    assert reasoning.rejects_reasoning_replay("opencode-go/glm-5.2") is True
    assert reasoning.rejects_reasoning_replay("glm-5.1") is True
    assert reasoning.rejects_reasoning_replay("mimo-v2.5") is False
    assert reasoning.rejects_reasoning_replay("kimi-k2.7-code") is False


# ---- the strip (through stream_model) ----

def _capture(monkeypatch, *, model_id, provider_id, messages, reasoning_effort=None):
    captured: dict[str, Any] = {}

    class _FakeProvider:
        async def aclose(self) -> None:
            pass

    class _FakeModel:
        provider = _FakeProvider()

    async def fake_stream_async(**kwargs: Any) -> ModelStreamResult:
        captured.update(kwargs)
        return ModelStreamResult(
            text="ok", tool_calls=[], reasoning="", usage=None,
            finish_reason="stop", assistant_message=ai.assistant_message("ok"),
        )

    monkeypatch.setattr(model_client, "resolve_model", lambda *a, **k: _FakeModel())
    monkeypatch.setattr(model_client, "_stream_async", fake_stream_async)
    model_client.stream_model(
        model_id=model_id, provider_id=provider_id, provider_base_url=None,
        provider_api_key="k", messages=messages, tools=None, max_output_tokens=64,
        reasoning_effort=reasoning_effort, on_text=lambda _c: None,
    )
    return captured


def _reasoning_history():
    return model_client.history_to_ai_messages(
        "sys",
        [
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "reasoning_content": "think first",
                "content": "",
                "tool_calls": [{"id": "c1", "type": "function",
                                "function": {"name": "ls", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "c1", "name": "ls", "content": "ok"},
        ],
    )


def _has_reasoning(messages) -> bool:
    return any(
        msg.role == "assistant"
        and any(isinstance(p, ai.types.messages.ReasoningPart) for p in msg.parts)
        for msg in messages
    )


def test_glm_strips_replayed_reasoning(monkeypatch):
    assert _has_reasoning(_reasoning_history())  # reconstructed by history_to_ai_messages
    captured = _capture(monkeypatch, model_id="glm-5.2", provider_id="opencode-go",
                        messages=_reasoning_history())
    assert not _has_reasoning(captured["messages"])
    # the tool_call survives the strip
    assert any(
        isinstance(p, ai.types.messages.ToolCallPart)
        for msg in captured["messages"] for p in msg.parts
    )


def test_mimo_keeps_replayed_reasoning(monkeypatch):
    captured = _capture(monkeypatch, model_id="mimo-v2.5", provider_id="mimo",
                        messages=_reasoning_history())
    assert _has_reasoning(captured["messages"])
