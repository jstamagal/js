from __future__ import annotations

import asyncio
import json

import ai
import httpx2

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
