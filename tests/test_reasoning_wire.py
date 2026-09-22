"""Capture actual SDK HTTP bodies, including llama.cpp's reasoning field contract."""

import asyncio
import json

import httpx2
import pytest

from js import model_client


@pytest.mark.parametrize(("provider", "base", "model", "field", "tool_free"), [
    ("openai", "http://local.test/v1", "qwen3.8-27b", "reasoning_content", True),
    ("llama.cpp", "http://127.0.0.1:8080/v1", "qwen3.8-27b", "reasoning_content", True),
    ("openai-completions", "http://custom.test/v1", "test", "reasoning_content", True),
    ("openai", "https://api.openai.com/v1", "test", "reasoning", True),
    ("mimo", "https://api.xiaomimimo.com/v1", "mimo-v2.5", "reasoning", True),
    ("opencode-go", "https://opencode.ai/zen/go/v1", "glm-5.2", None, False),
    ("deepseek", None, "deepseek-chat", "reasoning", False),
])
def test_reasoning_reaches_http_in_the_endpoint_field(monkeypatch, provider, base, model, field, tool_free):
    thought = " first thought\n  preserve whitespace: λ\n"
    messages = model_client.history_to_ai_messages("system", [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "answer", "reasoning_content": thought},
        {"role": "user", "content": "read"},
        {"role": "assistant", "content": "", "reasoning_content": thought,
         "tool_calls": [{"id": "call_1", "type": "function",
                         "function": {"name": "read", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_1", "name": "read", "content": "ok"},
        {"role": "user", "content": "next"},
    ], provider_id=provider)
    original = [m.model_dump() for m in messages]
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        chunk = {
            "id": "offline", "object": "chat.completion.chunk", "created": 1,
            "model": model,
            "choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}],
        }
        body = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode()
        return httpx2.Response(200, headers={"Content-Type": "text/event-stream"}, content=body)

    async def drive():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as client:
            get_provider = model_client.ai.get_provider

            def with_transport(*args, **kwargs):
                return get_provider(*args, **kwargs, client=client)

            monkeypatch.setattr(model_client.ai, "get_provider", with_transport)
            result = await model_client.stream_model_async(
                model_id=model, provider_id=provider, provider_base_url=base,
                provider_api_key="fixture", messages=messages, tools=None,
                max_output_tokens=None, reasoning_effort=None, on_text=lambda _chunk: None,
            )
            assert result.text == "ok"

    asyncio.run(drive())
    assert len(requests) == 1
    assistants = [m for m in requests[0]["messages"] if m["role"] == "assistant"]
    if field is None:
        assert all("reasoning" not in m and "reasoning_content" not in m for m in assistants)
    else:
        assert assistants[1][field] == thought
        if tool_free:
            assert assistants[0][field] == thought
        else:
            assert not assistants[0].get(field)
        other = "reasoning" if field == "reasoning_content" else "reasoning_content"
        assert all(other not in m for m in assistants)
    assert [m.model_dump() for m in messages] == original
