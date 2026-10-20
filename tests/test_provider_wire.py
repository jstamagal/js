from __future__ import annotations

import asyncio
import json

import ai
import httpx2
import pytest

from js import model_client


def _anthropic_reply():
    events = [
        {"type": "message_start", "message": {
            "id": "msg_1", "type": "message", "role": "assistant", "model": "test",
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 5, "output_tokens": 0},
        }},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ok"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
         "usage": {"output_tokens": 1}},
        {"type": "message_stop"},
    ]
    return httpx2.Response(
        200, headers={"content-type": "text/event-stream"},
        content="".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events),
    )


def _stream(monkeypatch, provider, respond, messages, **kwargs):
    async def drive():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as client:
            get_provider = model_client.ai.get_provider

            def with_transport(*args, **kwargs):
                return get_provider(*args, **kwargs, client=client)

            monkeypatch.setattr(model_client.ai, "get_provider", with_transport)
            return await model_client.stream_model_async(
                model_id="test", provider_id=provider, provider_base_url="https://offline.test/v1",
                provider_api_key="fixture", messages=messages, tools=None,
                max_output_tokens=None, reasoning_effort=None, on_text=lambda chunk: None,
                **kwargs,
            )

    return asyncio.run(drive())


def test_anthropic_tool_continuation_omits_empty_text_blocks(monkeypatch):
    messages = [
        ai.user_message("read"),
        ai.assistant_message(
            ai.types.messages.TextPart(text=""),
            ai.types.messages.ToolCallPart(tool_call_id="call_1", tool_name="read", tool_args="{}"),
        ),
        ai.tool_message(tool_call_id="call_1", tool_name="read", result="data"),
    ]
    original = [message.model_dump() for message in messages]

    def respond(request):
        wire = json.loads(request.content)["messages"]
        assert all(
            block.get("type") != "text" or block["text"]
            for message in wire if isinstance(message["content"], list)
            for block in message["content"]
        )
        assert wire[1]["content"] == [{"type": "tool_use", "id": "call_1", "name": "read", "input": {}}]
        assert wire[2]["content"] == [{"type": "tool_result", "tool_use_id": "call_1", "content": "data"}]
        return _anthropic_reply()

    assert _stream(monkeypatch, "anthropic", respond, messages).text == "ok"
    assert [message.model_dump() for message in messages] == original


def test_anthropic_retains_all_system_instructions(monkeypatch):
    messages = model_client.history_to_ai_messages("primary system", [
        {"role": "system", "content": "history system"},
        {"role": "user", "content": "ping"},
    ], provider_id="anthropic")
    original = [message.model_dump() for message in messages]

    def respond(request):
        wire = json.loads(request.content)
        assert "primary system" in wire["system"]
        assert "history system" in wire["system"]
        assert all(message["role"] != "system" for message in wire["messages"])
        return _anthropic_reply()

    assert _stream(monkeypatch, "anthropic", respond, messages).text == "ok"
    assert [message.model_dump() for message in messages] == original


@pytest.mark.parametrize("provider", ["openai", "openai-responses"])
def test_content_filter_marks_the_reply_incomplete(monkeypatch, provider):
    def respond(request):
        if provider == "openai":
            events = [{
                "id": "reply", "object": "chat.completion.chunk", "created": 1, "model": "test",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "content_filter"}],
            }]
        else:
            events = [{"type": "response.incomplete", "sequence_number": 0, "response": {
                "id": "reply", "object": "response", "created_at": 1, "model": "test",
                "status": "incomplete", "output": [], "error": None,
                "incomplete_details": {"reason": "content_filter"},
            }}]
        return httpx2.Response(
            200, headers={"content-type": "text/event-stream"},
            content="".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n",
        )

    result = _stream(monkeypatch, provider, respond, [ai.user_message("ping")])
    assert result.incomplete_reason == "content_filter"
    assert result.finish_reason == "incomplete:content_filter"
