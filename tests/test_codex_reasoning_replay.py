"""Codex encrypted reasoning items come back on the next request.

Requests ask for ``reasoning.encrypted_content``; the reasoning item that
carries it is kept on the reasoning part, stored in the session record, and
sent back as an input item to the same model. The request body is captured at
the provider's HTTP client.
"""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import ai
import httpx
import pytest

from js import codex_provider, memory, model_client, runtime
from js.config import from_env
from js.toolkit import ToolContext


_SUMMARY = [{"type": "summary_text", "text": "plan the answer"}]
_ITEM = {"type": "reasoning", "id": "rs_1", "summary": _SUMMARY, "encrypted_content": "ENC-1"}


def _fake_jwt() -> str:
    def seg(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    claims = {"https://api.openai.com/auth": {"chatgpt_account_id": "acct_1"}, "exp": 4102444800}
    return f"{seg({'alg': 'none'})}.{seg(claims)}.sig"


def _reasoning_events(*, summary: bool = True, text_first: bool = False) -> list[dict]:
    done = {"type": "response.output_item.done", "item": {**_ITEM, "status": "completed"}}
    events: list[dict] = [{"type": "response.output_item.added", "item": {"type": "reasoning", "id": "rs_1"}}]
    if summary:
        events.append({"type": "response.reasoning_summary_text.delta", "item_id": "rs_1",
                       "delta": "plan the answer"})
    text = {"type": "response.output_text.delta", "delta": "hello"}
    events += [text, done] if text_first else [done, text]
    events.append({"type": "response.completed", "response": {"usage": {"input_tokens": 3, "output_tokens": 2}}})
    return events


class _Response:
    status_code = 200

    def __init__(self, events: list[dict]):
        self.events = events

    async def aiter_lines(self):
        for event in self.events:
            yield f"event: {event['type']}"
            yield f"data: {json.dumps(event)}"
            yield ""


class _Context:
    def __init__(self, response: _Response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *exc):
        return False


class _Client:
    def __init__(self, scripts: list[list[dict]]):
        self.scripts = list(scripts)
        self.bodies: list[dict[str, Any]] = []

    def stream(self, method, url, *, headers=None, json=None):
        self.bodies.append(json)
        events = self.scripts.pop(0) if self.scripts else _reasoning_events()
        if isinstance(events, httpx.Response):
            return _Context(events)
        return _Context(_Response(events))

    async def aclose(self):
        return None


def _install(monkeypatch, client: _Client) -> None:
    monkeypatch.setattr(codex_provider, "_client_version", lambda: "0.145.0")

    def provider(*, provider_base_url, provider_api_key):
        return codex_provider.OpenAICodexProvider(
            access_token=_fake_jwt(), account_id="acct_1",
            base_url="https://chatgpt.com/backend-api", client=client,
        )

    monkeypatch.setattr(codex_provider, "provider_from_login_or_token", provider)


def _stream(monkeypatch, events: list[dict], messages=None) -> tuple[model_client.ModelStreamResult, dict]:
    client = _Client([events])
    _install(monkeypatch, client)
    result = model_client.stream_model(
        model_id="gpt-5.5", provider_id="openai-codex",
        provider_base_url="https://chatgpt.com/backend-api", provider_api_key=_fake_jwt(),
        messages=messages or [ai.user_message("ping")], tools=None, max_output_tokens=None,
        reasoning_effort="high", on_text=lambda _c: None,
    )
    return result, client.bodies[0]


def test_the_request_asks_for_encrypted_reasoning(monkeypatch):
    _result, body = _stream(monkeypatch, _reasoning_events())

    assert body["include"] == ["reasoning.encrypted_content"]


@pytest.mark.parametrize(("summary", "text_first", "text"), [
    (True, False, "plan the answer"),
    (False, False, ""),
    (True, True, "plan the answer"),
])
def test_the_reasoning_part_carries_the_encrypted_item(monkeypatch, summary, text_first, text):
    result, _body = _stream(monkeypatch, _reasoning_events(summary=summary, text_first=text_first))

    assert result.text == "hello"
    assert model_client.signed_reasoning_parts(result.assistant_message) == [
        {"text": text, "provider_metadata": {"openai-codex": {"item": _ITEM}}},
    ]


def _record(model: str = "gpt-5.5", **extra) -> dict:
    return {
        "role": "assistant", "content": "hello", "reasoning_content": "plan the answer",
        "reasoning_parts": [{"text": "plan the answer", "provider_metadata": {"openai-codex": {"item": _ITEM}}}],
        "reasoning_from": {"provider": "openai-codex", "model": model},
        **extra,
    }


def _body_for(history: list[dict], *, model: str = "gpt-5.5") -> dict:
    messages = model_client.history_to_ai_messages(
        "SYSTEM", history, provider_id="openai-codex", model_id=model,
    )
    return asyncio.run(codex_provider._build_body_async(
        SimpleNamespace(id=model), messages, None, None,
    ))


def test_a_stored_reasoning_item_goes_back_ahead_of_its_message():
    body = _body_for([{"role": "user", "content": "ping"}, _record(), {"role": "user", "content": "next"}])

    assert body["input"][1:3] == [
        _ITEM,
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hello"}]},
    ]


def test_a_stored_reasoning_item_goes_back_ahead_of_its_tool_call():
    call = {"id": "call_1", "type": "function", "function": {"name": "read", "arguments": "{}"}}
    history = [
        {"role": "user", "content": "ping"},
        _record(content="", tool_calls=[call]),
        {"role": "tool", "tool_call_id": "call_1", "name": "read", "content": "ok"},
    ]
    body = _body_for(history)

    assert [item["type"] for item in body["input"][1:]] == ["reasoning", "function_call", "function_call_output"]
    assert body["input"][1] == _ITEM


def test_commentary_between_reasoning_items_replays_in_the_order_it_came():
    second = {**_ITEM, "id": "rs_2", "encrypted_content": "ENC-2"}
    live = ai.assistant_message(
        ai.thinking("a", provider_metadata={"openai-codex": {"item": _ITEM}}),
        ai.types.messages.TextPart(text="checking"),
        ai.thinking("b", provider_metadata={"openai-codex": {"item": second}}),
        ai.types.messages.ToolCallPart(tool_call_id="call_1", tool_name="read", tool_args="{}"),
    )
    record = {
        "role": "assistant", "content": "checking",
        "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "read", "arguments": "{}"}}],
        "reasoning_parts": model_client.signed_reasoning_parts(live),
        "reasoning_from": {"provider": "openai-codex", "model": "gpt-5.5"},
    }
    live_body = asyncio.run(codex_provider._build_body_async(
        SimpleNamespace(id="gpt-5.5"), [ai.user_message("ping"), live], None, None))
    replayed = _body_for([{"role": "user", "content": "ping"}, record])

    assert [item["type"] for item in replayed["input"][1:5]] == ["reasoning", "message", "reasoning", "function_call"]
    assert replayed["input"] == live_body["input"]


def test_a_reasoning_item_is_not_sent_to_another_model():
    body = _body_for([{"role": "user", "content": "ping"}, _record(model="gpt-5.5")], model="gpt-5.4")

    assert all(item["type"] != "reasoning" for item in body["input"])


def test_a_reasoning_item_with_nothing_after_it_is_left_out():
    body = _body_for([{"role": "user", "content": "ping"}, _record(content="")])

    assert all(item["type"] != "reasoning" for item in body["input"])


@pytest.fixture
def codex(monkeypatch, tmp_path):
    cfg = from_env(
        agent_id="codex-replay",
        session="codex-replay",
        extras=[
            "model.id=gpt-5.5",
            "provider.id=openai-codex",
            "provider.api_key=" + _fake_jwt(),
            "model.reasoning_effort=high",
            "model.vision=off",
            "model.context_window=200000",
            "runtime.debug_autolog=off",
            "runtime.transcript_log=off",
        ],
    )
    prompts = tmp_path / ".js" / "agents" / "codex-replay"
    prompts.mkdir(parents=True)
    (prompts / "01-prompt.md").write_text("SYSTEM\n")
    cfg = replace(cfg, prompts_dir=prompts, project_dir=tmp_path)
    context = ToolContext(cwd=tmp_path)
    monkeypatch.setattr(runtime.T, "STOCK_CONTEXT", context)
    client = _Client([])
    _install(monkeypatch, client)
    return cfg, client, context


def test_a_resumed_codex_session_sends_the_reasoning_item_back(codex):
    cfg, client, context = codex
    messages = [{"role": "user", "content": "ping"}]
    runtime.run_turn(cfg, "SYSTEM", messages, runtime.Telemetry(None),
                     tool_context=context, trace_override=False, suppress_output=True)
    memory.persist_messages(cfg.session_file, messages, memory.stamp_for("gpt-5.5", "openai-codex", "high"))

    resumed = memory.load_replay_messages(cfg.session_file)
    resumed.append({"role": "user", "content": "next"})
    runtime.run_turn(cfg, "SYSTEM", resumed, runtime.Telemetry(None),
                     tool_context=context, trace_override=False, suppress_output=True)

    second = client.bodies[1]["input"]
    assert second[1] == _ITEM
    assert second[2]["type"] == "message" and second[2]["role"] == "assistant"


def test_a_reasoning_item_the_endpoint_cannot_decrypt_is_dropped_and_retried(codex):
    cfg, client, context = codex
    messages = [{"role": "user", "content": "ping"}]
    runtime.run_turn(cfg, "SYSTEM", messages, runtime.Telemetry(None),
                     tool_context=context, trace_override=False, suppress_output=True)
    refusal = httpx.Response(400, request=httpx.Request("POST", "https://chatgpt.com/backend-api/codex/responses"),
                             json={"error": {"message": "The encrypted content for item rs_1 could not be verified.",
                                             "type": "invalid_request_error", "code": "invalid_encrypted_content"}})
    client.scripts = [refusal]
    messages.append({"role": "user", "content": "next"})
    runtime.run_turn(cfg, "SYSTEM", messages, runtime.Telemetry(None),
                     tool_context=context, trace_override=False, suppress_output=True)

    assert client.bodies[1]["input"][1] == _ITEM
    assert all(item["type"] != "reasoning" for item in client.bodies[2]["input"])
    assert messages[-1]["role"] == "assistant" and messages[-1]["content"] == "hello"
