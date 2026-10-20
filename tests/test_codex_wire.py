from __future__ import annotations

import asyncio
import base64
import json

import ai
import httpx

from js import codex_provider, logins, model_client


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


def test_explicit_codex_token_selects_its_own_account(monkeypatch):
    claims = {"https://api.openai.com/auth": {"chatgpt_account_id": "acct_explicit"}}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    token = f"header.{payload}.signature"
    saved = logins.Login(
        provider_id="openai-codex", provider_api_key="saved-token",
        codex_account_id="acct_saved", codex_refresh_token="saved-refresh",
        codex_token_expires=4102444800,
    )
    monkeypatch.setattr(logins, "load_logins", lambda: {"openai-codex": saved})

    def respond(request):
        assert request.headers["authorization"] == f"Bearer {token}"
        assert request.headers["chatgpt-account-id"] == "acct_explicit"
        return _completed()

    async def drive():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            monkeypatch.setattr(codex_provider.httpx, "AsyncClient", lambda **kwargs: client)
            await model_client.stream_model_async(
                model_id="test", provider_id="openai-codex", provider_base_url=None,
                provider_api_key=token, messages=[ai.user_message("ping")], tools=None,
                max_output_tokens=None, reasoning_effort=None, on_text=lambda chunk: None,
            )

    asyncio.run(drive())


def test_superseded_token_of_the_saved_account_uses_the_saved_login(monkeypatch):
    claims = {"https://api.openai.com/auth": {"chatgpt_account_id": "acct_saved"}}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    superseded = f"header.{payload}.old-signature"
    saved = logins.Login(
        provider_id="openai-codex", provider_api_key="rotated-token",
        codex_account_id="acct_saved", codex_refresh_token="saved-refresh",
        codex_token_expires=4102444800,
    )
    monkeypatch.setattr(logins, "load_logins", lambda: {"openai-codex": saved})

    provider = codex_provider.provider_from_login_or_token(
        provider_base_url=None, provider_api_key=superseded,
    )

    assert asyncio.run(provider._ensure_access()) == ("rotated-token", "acct_saved")


def test_streamed_text_items_keep_the_reasoning_between_them(monkeypatch):
    reasoning = {
        "type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "signed",
    }
    events = [
        {"type": "response.output_item.added", "item": {"type": "message", "id": "msg_1"}},
        {"type": "response.output_text.delta", "item_id": "msg_1", "delta": "before"},
        {"type": "response.output_item.done", "item": {"type": "message", "id": "msg_1"}},
        {"type": "response.output_item.added", "item": {"type": "reasoning", "id": "rs_1"}},
        {"type": "response.reasoning_summary_text.delta", "item_id": "rs_1", "delta": "plan"},
        {"type": "response.output_item.done", "item": reasoning},
        {"type": "response.output_item.added", "item": {"type": "message", "id": "msg_2"}},
        {"type": "response.output_text.delta", "item_id": "msg_2", "delta": "after"},
        {"type": "response.output_item.done", "item": {"type": "message", "id": "msg_2"}},
        {"type": "response.completed", "response": {"status": "completed"}},
    ]

    def respond(request):
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            content="".join(f"data: {json.dumps(event)}\n\n" for event in events),
        )

    result = _stream(monkeypatch, respond, [ai.user_message("ping")])
    assert [(part.kind, part.text) for part in result.assistant_message.parts] == [
        ("text", "before"), ("reasoning", "plan"), ("text", "after"),
    ]
    assert model_client.signed_reasoning_parts(result.assistant_message)[0]["text_before"] == 6


def test_registered_codex_settings_reach_the_request(monkeypatch):
    seen = []

    def respond(request):
        seen.append(request)
        return _completed()

    _stream(monkeypatch, respond, [ai.user_message("hi")], settings={
        "CODEX_TEXT_VERBOSITY": "high", "CODEX_ORIGINATOR": "custom",
        "CODEX_USER_AGENT": "configured-agent",
    })
    assert json.loads(seen[0].content)["text"]["verbosity"] == "high"
    assert seen[0].headers["originator"] == "custom"
    assert seen[0].headers["user-agent"] == "configured-agent"
