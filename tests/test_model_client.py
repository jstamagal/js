"""Tests for the ai-python SDK boundary in js/model_client.py.

No network calls. All model interactions use a fake ``StreamExecutor``.
"""

from __future__ import annotations

import ai
import ai.types.events
import ai.types.messages
import ai.types.tools
import ai.types.usage
import ai.models
import pytest

from js import model_client


class _FakeExecutor:
    """Yields scripted events for a single stream."""

    def __init__(self, events):
        self.events = events
        self.request = None

    async def _do_stream(self, request: ai.models.StreamRequest):
        self.request = request
        for event in self.events:
            yield event


def _text_events(text: str) -> list[ai.types.events.Event]:
    return [
        ai.types.events.StreamStart(),
        ai.types.events.TextStart(block_id="text"),
        ai.types.events.TextDelta(chunk=text, block_id="text"),
        ai.types.events.TextEnd(block_id="text"),
        ai.types.events.StreamEnd(),
    ]


def _tool_events(tool_call_id: str, name: str, args: str) -> list[ai.types.events.Event]:
    return [
        ai.types.events.StreamStart(),
        ai.types.events.ToolStart(tool_call_id=tool_call_id, tool_name=name),
        ai.types.events.ToolDelta(chunk=args, tool_call_id=tool_call_id),
        ai.types.events.ToolEnd(
            tool_call_id=tool_call_id,
            tool_call=ai.types.messages.DUMMY_TOOL_CALL,
        ),
        ai.types.events.StreamEnd(),
    ]


def _reasoning_events(text: str) -> list[ai.types.events.Event]:
    return [
        ai.types.events.StreamStart(),
        ai.types.events.ReasoningStart(block_id="reasoning"),
        ai.types.events.ReasoningDelta(chunk=text, block_id="reasoning"),
        ai.types.events.ReasoningEnd(block_id="reasoning"),
        ai.types.events.StreamEnd(),
    ]


def _usage_events(
    input_tokens: int = 100,
    output_tokens: int = 10,
    cache_read_tokens: int = 25,
) -> list[ai.types.events.Event]:
    return [
        ai.types.events.StreamStart(),
        ai.types.events.TextStart(block_id="text"),
        ai.types.events.TextDelta(chunk="done", block_id="text"),
        ai.types.events.TextEnd(block_id="text"),
        ai.types.events.StreamEnd(
            usage=ai.types.usage.Usage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens,
            )
        ),
    ]


def test_resolve_model_uses_gateway_when_provider_unset(monkeypatch):
    captured = {}

    def fake_get_model(model_id: str) -> ai.Model:
        captured["model_id"] = model_id
        return ai.Model("fake-id", provider=ai.get_provider("openai", api_key="x"))

    monkeypatch.setattr(ai, "get_model", fake_get_model)

    result = model_client.resolve_model(
        "deepseek/deepseek-v4-flash",
        provider_id=None,
        provider_base_url=None,
        provider_api_key=None,
    )
    assert captured["model_id"] == "deepseek/deepseek-v4-flash"
    assert result.id == "fake-id"


def test_resolve_model_uses_explicit_provider_verbatim():
    result = model_client.resolve_model(
        "gemma4:e4b",
        provider_id="openai",
        provider_base_url="http://127.0.0.1:11434/v1",
        provider_api_key="ollama",
    )
    assert result.id == "gemma4:e4b"
    assert result.provider.name == "openai"
    assert result.provider.base_url == "http://127.0.0.1:11434/v1"


def test_resolve_model_rejects_model_not_served_by_provider():
    with pytest.raises(ValueError, match="opencode-go-anthropic does not serve model glm-5.1"):
        model_client.resolve_model(
            "glm-5.1",
            provider_id="opencode-go-anthropic",
            provider_base_url="https://opencode.ai/zen/go",
            provider_api_key="sk-test",
        )


def test_tool_specs_to_ai_tools_requires_function_type():
    with pytest.raises(ValueError, match="unsupported tool spec type"):
        model_client.tool_specs_to_ai_tools([{"type": "provider"}])


def test_tool_specs_to_ai_tools_requires_name():
    with pytest.raises(ValueError, match="missing function.name"):
        model_client.tool_specs_to_ai_tools([{"type": "function", "function": {}}])


def test_tool_specs_to_ai_tools_converts_spec():
    specs = [
        {
            "type": "function",
            "function": {
                "name": "fs_search",
                "description": "Search files",
                "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
            },
        }
    ]
    tools = model_client.tool_specs_to_ai_tools(specs)
    assert len(tools) == 1
    tool = tools[0]
    assert isinstance(tool, ai.types.tools.Tool)
    assert tool.name == "fs_search"
    assert isinstance(tool.args, ai.types.tools.FunctionToolArgs)
    assert tool.args.description == "Search files"
    assert tool.args.params["type"] == "object"


def test_history_to_ai_messages_preserves_roles():
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello", "reasoning_content": "stripped"},
        {
            "role": "assistant",
            "content": "call",
            "reasoning_content": "needed",
            "tool_calls": [
                {
                    "id": "tc1",
                    "type": "function",
                    "function": {"name": "fs_search", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "tc1", "name": "fs_search", "content": "42"},
    ]
    out = model_client.history_to_ai_messages("sys", msgs)
    assert [m.role for m in out] == [
        "system",
        "user",
        "assistant",
        "assistant",
        "tool",
    ]
    # Tool-free assistant reasoning is archive-only and should not be replayed.
    assert [p.kind for p in out[2].parts] == ["text"]
    # Assistant reasoning is replayed when the same assistant turn carried tool calls.
    tc_parts = out[3].parts
    assert [p.kind for p in tc_parts] == ["reasoning", "text", "tool_call"]
    assert tc_parts[2].tool_name == "fs_search"


def test_stream_model_collects_text_reasoning_tool_calls_and_usage():
    events = [
        ai.types.events.StreamStart(),
        ai.types.events.TextStart(block_id="text"),
        ai.types.events.TextDelta(chunk="DONE", block_id="text"),
        ai.types.events.TextEnd(block_id="text"),
        ai.types.events.ReasoningStart(block_id="reasoning"),
        ai.types.events.ReasoningDelta(chunk="thinking", block_id="reasoning"),
        ai.types.events.ReasoningEnd(block_id="reasoning"),
        ai.types.events.ToolStart(tool_call_id="tc1", tool_name="fs_search"),
        ai.types.events.ToolDelta(chunk='{"q":"x"}', tool_call_id="tc1"),
        ai.types.events.ToolEnd(
            tool_call_id="tc1",
            tool_call=ai.types.messages.DUMMY_TOOL_CALL,
        ),
        ai.types.events.StreamEnd(
            usage=ai.types.usage.Usage(
                input_tokens=100,
                output_tokens=10,
                cache_read_tokens=25,
            )
        ),
    ]
    executor = _FakeExecutor(events)
    emitted_text: list[str] = []
    result = model_client.stream_model(
        model_id="test",
        provider_id="openai",
        provider_base_url="http://localhost:11434/v1",
        provider_api_key="x",
        messages=[ai.user_message("hi")],
        tools=None,
        max_output_tokens=100,
        reasoning_effort=None,
        on_text=emitted_text.append,
        executor=executor,
    )
    assert result.text == "DONE"
    assert result.reasoning == "thinking"
    assert result.finish_reason == "tool_calls"
    assert len(result.tool_calls) == 1
    tc = result.tool_calls[0]
    assert tc.id == "tc1"
    assert result.usage.output_tokens == 10
    assert emitted_text == ["DONE"]
    assert executor.request is not None
    assert executor.request.params == {"max_tokens": 100}


def test_stream_model_passes_reasoning_effort():
    executor = _FakeExecutor(_text_events("ok"))
    model_client.stream_model(
        model_id="test",
        provider_id="openai",
        provider_base_url="http://localhost:11434/v1",
        provider_api_key="x",
        messages=[ai.user_message("hi")],
        tools=None,
        max_output_tokens=64,
        reasoning_effort="low",
        on_text=lambda _s: None,
        executor=executor,
    )
    assert executor.request.params == {
        "max_tokens": 64,
        "reasoning_effort": "low",
    }


def test_stream_model_passes_reasoning_none():
    executor = _FakeExecutor(_text_events("ok"))
    model_client.stream_model(
        model_id="test",
        provider_id="openai",
        provider_base_url="http://localhost:11434/v1",
        provider_api_key="x",
        messages=[ai.user_message("hi")],
        tools=None,
        max_output_tokens=64,
        reasoning_effort="none",
        on_text=lambda _s: None,
        executor=executor,
    )
    assert executor.request.params == {
        "max_tokens": 64,
    }


def test_stream_model_puts_deepseek_reasoning_budget_in_extra_body():
    executor = _FakeExecutor(_text_events("ok"))
    model_client.stream_model(
        model_id="deepseek-chat",
        provider_id="deepseek",
        provider_base_url=None,
        provider_api_key="x",
        messages=[ai.user_message("hi")],
        tools=None,
        max_output_tokens=64,
        reasoning_effort=None,
        on_text=lambda _s: None,
        executor=executor,
    )
    assert executor.request.params == {
        "max_tokens": 64,
        "extra_body": {"max_reasoning_tokens": 32_000},
    }


def test_stream_model_strips_minimax_reasoning_by_model_prefix():
    executor = _FakeExecutor(_text_events("ok"))
    model_client.stream_model(
        model_id="minimax:text-01",
        provider_id=None,
        provider_base_url=None,
        provider_api_key=None,
        messages=[ai.user_message("hi")],
        tools=None,
        max_output_tokens=64,
        reasoning_effort="high",
        on_text=lambda _s: None,
        executor=executor,
    )
    assert executor.request.params == {"max_tokens": 64}

def test_image_tool_result_becomes_user_file_message_without_persisting_base64(tmp_path):
    image = tmp_path / "img.png"
    image.write_bytes(b"\x89PNGfake")
    result = f"IMAGE_RESULT\t{image}\timage/png\tvisual stub"
    messages = model_client.build_tool_result_messages("tc1", "read_visual", result)
    assert [m.role for m in messages] == ["tool", "user"]
    tool_part = messages[0].parts[0]
    assert tool_part.kind == "tool_result"
    assert tool_part.tool_call_id == "tc1"
    assert tool_part.result == "visual stub"
    user_parts = messages[1].parts
    assert isinstance(user_parts[0], ai.types.messages.TextPart)
    assert user_parts[0].text == "visual stub"
    assert isinstance(user_parts[1], ai.types.messages.FilePart)
    assert user_parts[1].media_type == "image/png"
    assert isinstance(user_parts[1].data, bytes)

def test_plain_tool_result_message():
    msg = model_client.build_tool_result_message("tc1", "fs_search", "42")
    assert msg.role == "tool"
    assert msg.parts[0].kind == "tool_result"
    assert msg.parts[0].tool_call_id == "tc1"
    assert msg.parts[0].result == "42"
    assert msg.parts[0].tool_name == "fs_search"
    assert not msg.parts[0].is_error


def test_error_tool_result_message():
    msg = model_client.build_tool_result_message("tc1", "fs_search", "ERROR: disk full")
    assert msg.parts[0].is_error
    assert msg.parts[0].result == "ERROR: disk full"


def test_history_messages_can_be_roundtripped():
    """Provider-facing messages from history_to_ai_messages can be serialized and rebuilt."""
    msgs = [
        {"role": "assistant", "content": "hello", "reasoning_content": "hmm"},
        {"role": "tool", "tool_call_id": "tc1", "name": "fs_search", "content": "42"},
    ]
    out = model_client.history_to_ai_messages("sys", msgs)
    # Messages should survive pydantic serialization (used internally by ai)
    serialized = [m.model_dump() for m in out]
    assert len(serialized) == 3
    assert serialized[0]["role"] == "system"
    assert serialized[1]["role"] == "assistant"
    assert serialized[2]["role"] == "tool"
