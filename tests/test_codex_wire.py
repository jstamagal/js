from __future__ import annotations

import asyncio
import json

import ai
import httpx

from js import codex_provider, model_client


def _completed():
    event = {"type": "response.completed", "response": {"status": "completed"}}
    return httpx.Response(
        200, headers={"content-type": "text/event-stream"},
        content=f"data: {json.dumps(event)}\n\n",
    )


def _stream(monkeypatch, respond, messages, **kwargs):
    async def drive():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            provider = codex_provider.OpenAICodexProvider(
                access_token="fixture", account_id="acct_fixture", client=client,
            )
            monkeypatch.setattr(
                model_client, "resolve_model", lambda *a, **kw: ai.Model(id="test", provider=provider),
            )
            return await model_client.stream_model_async(
                model_id="test", provider_id="openai-codex", provider_base_url=None,
                provider_api_key="fixture", messages=messages, tools=None,
                max_output_tokens=None, reasoning_effort=None, on_text=lambda chunk: None,
                **kwargs,
            )

    return asyncio.run(drive())


def test_assistant_text_stays_on_each_side_of_encrypted_reasoning(monkeypatch):
    reasoning = {
        "type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "signed",
    }
    message = ai.assistant_message(
        ai.types.messages.TextPart(text="before"),
        ai.thinking("", provider_metadata={"openai-codex": {"item": reasoning}}),
        ai.types.messages.TextPart(text="after"),
    )

    def respond(request):
        items = json.loads(request.content)["input"]
        assert items == [
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "before"}]},
            reasoning,
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "after"}]},
        ]
        return _completed()

    _stream(monkeypatch, respond, [message])
