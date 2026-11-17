"""A model reply as a live turn keeps it, and as a resumed session rebuilds it.

Each case runs one turn against a mock endpoint, so the provider's SDK
parses a real stream body. The turn appends the reply twice: as an SDK
message to its convo, which the next model call of the same turn sends, and
as a history record. A resumed session saves and loads that record and sends
what `history_to_ai_messages` builds from it.

`fixtures/assistant_record_replay/<case>.json` pins, for every case, the
record, the live SDK message, the rebuilt SDK message and, when the turn made a
second model call, the assistant entries of that request body next to those of
the body a resumed session sends for the same history. UPDATE_SNAPSHOTS=1
writes them from the current code.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path

import httpx2
import pytest
from turn_driver import one_turn

from js import history_record, memory, model_client, runtime
from js.config import InvocationSources, from_env
from js.toolkit import ToolContext

SNAPSHOTS = Path(__file__).parent / "fixtures" / "assistant_record_replay"
SYSTEM = "SYSTEM"


def _sse(events: list[tuple[str | None, dict | str]]) -> bytes:
    lines = []
    for name, data in events:
        payload = data if isinstance(data, str) else json.dumps(data)
        lines.append((f"event: {name}\n" if name else "") + f"data: {payload}\n\n")
    return "".join(lines).encode()


# Anthropic Messages -------------------------------------------------------


def _anthropic(blocks: list[dict], stop: str) -> bytes:
    """A reply whose content is ``blocks``, as Anthropic streams it. A
    tool_use block's ``raw`` is its streamed input JSON, cut or whole."""
    events: list[dict] = [{"type": "message_start", "message": {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5", "content": [],
        "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 5, "output_tokens": 1}}}]
    for n, block in enumerate(blocks):
        kind = block["type"]
        if kind == "thinking":
            events.append({"type": "content_block_start", "index": n,
                           "content_block": {"type": "thinking", "thinking": "", "signature": ""}})
            if block["thinking"]:
                events.append({"type": "content_block_delta", "index": n,
                               "delta": {"type": "thinking_delta", "thinking": block["thinking"]}})
            events.append({"type": "content_block_delta", "index": n,
                           "delta": {"type": "signature_delta", "signature": block["signature"]}})
        elif kind == "redacted_thinking":
            events.append({"type": "content_block_start", "index": n, "content_block": block})
        elif kind == "text":
            events += [{"type": "content_block_start", "index": n, "content_block": {"type": "text", "text": ""}},
                       {"type": "content_block_delta", "index": n,
                        "delta": {"type": "text_delta", "text": block["text"]}}]
        else:
            raw = block.get("raw", json.dumps(block.get("input", {})))
            events += [{"type": "content_block_start", "index": n, "content_block": {
                           "type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}},
                       {"type": "content_block_delta", "index": n, "delta": {
                           "type": "input_json_delta", "partial_json": raw}}]
        events.append({"type": "content_block_stop", "index": n})
    events += [{"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                "usage": {"output_tokens": 5}},
               {"type": "message_stop"}]
    return _sse([(event["type"], event) for event in events])


def _anthropic_ok() -> bytes:
    return _anthropic([{"type": "text", "text": "ok"}], "end_turn")


# OpenAI chat completions --------------------------------------------------


def _chat(deltas: list[dict], finish: str) -> bytes:
    chunks = [{"id": "chat_1", "object": "chat.completion.chunk", "created": 1, "model": "m",
               "choices": [{"index": 0, "delta": delta, "finish_reason": None}]} for delta in deltas]
    chunks.append({"id": "chat_1", "object": "chat.completion.chunk", "created": 1, "model": "m",
                   "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                   "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10}})
    return _sse([(None, chunk) for chunk in chunks] + [(None, "[DONE]")])


def _chat_ok() -> bytes:
    return _chat([{"role": "assistant", "content": "ok"}], "stop")


def _chat_call(call_id: str, name: str, arguments: str, index: int = 0) -> dict:
    return {"tool_calls": [{"index": index, "id": call_id, "type": "function",
                            "function": {"name": name, "arguments": arguments}}]}


# OpenAI Responses ---------------------------------------------------------


def _responses(items: list[dict], *, partial_images: tuple[tuple[str, str], ...] = ()) -> bytes:
    """A Responses stream whose output is ``items``, each added then done.
    ``partial_images`` are (item id, base64) partial images streamed for an
    image_generation_call item."""
    response = {"id": "resp_1", "object": "response", "created_at": 1, "model": "m", "status": "in_progress",
                "output": [], "usage": None}
    events: list[dict] = [{"type": "response.created", "sequence_number": 0, "response": response}]
    seq = 1
    for index, item in enumerate(items):
        added = {**item, "status": "in_progress"}
        if item["type"] == "message":
            added["content"] = []
        events.append({"type": "response.output_item.added", "sequence_number": seq,
                       "output_index": index, "item": added})
        seq += 1
        if item["type"] == "message":
            for part in item["content"]:
                events.append({"type": "response.output_text.delta", "sequence_number": seq,
                               "item_id": item["id"], "output_index": index, "content_index": 0,
                               "delta": part["text"], "logprobs": []})
                seq += 1
        for item_id, image in partial_images:
            if item_id == item.get("id"):
                events.append({"type": "response.image_generation_call.partial_image", "sequence_number": seq,
                               "item_id": item_id, "output_index": index, "partial_image_index": 0,
                               "partial_image_b64": image})
                seq += 1
        events.append({"type": "response.output_item.done", "sequence_number": seq,
                       "output_index": index, "item": {**item, "status": "completed"}})
        seq += 1
    done = {**response, "status": "completed", "output": [{**item, "status": "completed"} for item in items],
            "usage": {"input_tokens": 5, "output_tokens": 5, "total_tokens": 10,
                      "input_tokens_details": {"cached_tokens": 0},
                      "output_tokens_details": {"reasoning_tokens": 0}}}
    events.append({"type": "response.completed", "sequence_number": seq, "response": done})
    return _sse([(event["type"], event) for event in events])


def _responses_message(item_id: str, text: str) -> dict:
    return {"type": "message", "id": item_id, "role": "assistant",
            "content": [{"type": "output_text", "text": text, "annotations": []}]}


def _responses_ok() -> bytes:
    return _responses([_responses_message("msg_ok", "ok")])


# The cases ----------------------------------------------------------------


@dataclass(frozen=True)
class Case:
    provider: str
    model: str
    base: str | None
    replies: tuple[bytes, ...]
    extras: tuple[str, ...] = ()
    # The text a body carries the conversation in.
    conversation_key: str = "messages"
    settings: dict = field(default_factory=dict)


_ANTHROPIC = ("anthropic", "claude-sonnet-5-5", None)
_LLAMA = ("llama.cpp", "qwen-test", "http://127.0.0.1:8080/v1")
_DEEPSEEK = ("deepseek", "deepseek-chat", "https://api.deepseek.com")
_CPA = ("cliproxyapi", "claude-sonnet-5-5", "http://cpa.test/v1")
_RESPONSES = ("openai-responses", "gpt-test", "http://responses.test/v1")


def _case(route: tuple, *replies: bytes, **kwargs) -> Case:
    provider, model, base = route
    return Case(provider, model, base, replies, **kwargs)


CASES: dict[str, Case] = {
    "anthropic_text_signed": _case(_ANTHROPIC, _anthropic([
        {"type": "thinking", "thinking": "weigh it", "signature": "S1"},
        {"type": "text", "text": "the answer"},
    ], "end_turn")),
    "anthropic_tool_call_signed": _case(_ANTHROPIC, _anthropic([
        {"type": "thinking", "thinking": "weigh it", "signature": "S1"},
        {"type": "tool_use", "id": "toolu_1", "name": "no_such_tool", "input": {"path": "a"}},
    ], "tool_use"), _anthropic_ok()),
    "anthropic_text_before_tool_call": _case(_ANTHROPIC, _anthropic([
        {"type": "thinking", "thinking": "weigh it", "signature": "S1"},
        {"type": "text", "text": "Reading the file."},
        {"type": "tool_use", "id": "toolu_1", "name": "no_such_tool", "input": {"path": "a"}},
    ], "tool_use"), _anthropic_ok()),
    "anthropic_interleaved": _case(_ANTHROPIC, _anthropic([
        {"type": "thinking", "thinking": "", "signature": "S1"},
        {"type": "text", "text": "Reading the first file."},
        {"type": "tool_use", "id": "toolu_1", "name": "no_such_tool", "input": {"path": "a"}},
        {"type": "text", "text": "Now the second."},
        {"type": "thinking", "thinking": "progress note", "signature": "S2"},
        {"type": "tool_use", "id": "toolu_2", "name": "no_such_tool", "input": {"path": "b"}},
    ], "tool_use"), _anthropic_ok()),
    "anthropic_redacted_thinking": _case(_ANTHROPIC, _anthropic([
        {"type": "redacted_thinking", "data": "opaque"},
        {"type": "thinking", "thinking": "weigh it", "signature": "S1"},
        {"type": "tool_use", "id": "toolu_1", "name": "no_such_tool", "input": {"path": "a"}},
    ], "tool_use"), _anthropic_ok()),
    "anthropic_capped_batch": _case(_ANTHROPIC, _anthropic([
        {"type": "thinking", "thinking": "weigh it", "signature": "S1"},
        {"type": "text", "text": "A"},
        {"type": "tool_use", "id": "toolu_1", "name": "no_such_tool", "input": {"path": "a"}},
        {"type": "text", "text": "B"},
        {"type": "tool_use", "id": "toolu_2", "name": "no_such_tool", "input": {"path": "b"}},
    ], "tool_use"), _anthropic_ok(), extras=("REPLY_TOOL_CALLS_MAX=1",)),
    "anthropic_cut_off_text": _case(_ANTHROPIC, _anthropic([
        {"type": "thinking", "thinking": "weigh it", "signature": "S1"},
        {"type": "text", "text": "the first half"},
    ], "max_tokens"), _anthropic_ok()),
    "anthropic_cut_off_tool_args": _case(_ANTHROPIC, _anthropic([
        {"type": "thinking", "thinking": "weigh it", "signature": "S1"},
        {"type": "text", "text": "Writing it."},
        {"type": "tool_use", "id": "toolu_1", "name": "no_such_tool", "raw": '{"path": "a'},
    ], "max_tokens"), _anthropic_ok()),
    "chat_text_reasoning": _case(_LLAMA, _chat([
        {"role": "assistant", "reasoning_content": "weigh it"},
        {"content": "the answer"},
    ], "stop")),
    "chat_tool_call_reasoning": _case(_LLAMA, _chat([
        {"role": "assistant", "reasoning_content": "weigh it"},
        {"content": "Reading the file."},
        _chat_call("call_1", "no_such_tool", '{"path": "a"}'),
    ], "tool_calls"), _chat_ok()),
    "chat_tool_call_bad_arguments": _case(_LLAMA, _chat([
        {"role": "assistant", "content": ""},
        _chat_call("call_1", "no_such_tool", '{"path": "a",}'),
    ], "tool_calls"), _chat_ok()),
    "chat_cut_off_text": _case(_LLAMA, _chat([
        {"role": "assistant", "reasoning_content": "weigh it"},
        {"content": "the first half"},
    ], "length"), _chat_ok()),
    "deepseek_cut_off_text": _case(_DEEPSEEK, _chat([
        {"role": "assistant", "reasoning_content": "weigh it"},
        {"content": "the first half"},
    ], "length"), _chat_ok()),
    "deepseek_tool_call_reasoning": _case(_DEEPSEEK, _chat([
        {"role": "assistant", "reasoning_content": "weigh it"},
        _chat_call("call_1", "no_such_tool", '{"path": "a"}'),
    ], "tool_calls"), _chat_ok()),
    "cpa_reasoning_blocks_tool_call": _case(_CPA, _chat([
        {"role": "assistant", "reasoning_content": "weigh it",
         "reasoning_blocks": [{"index": 0, "type": "thinking", "thinking": "weigh it"}]},
        {"reasoning_blocks": [{"index": 0, "type": "thinking", "signature": "S1"}]},
        _chat_call("call_1", "no_such_tool", '{"path": "a"}'),
    ], "tool_calls"), _chat_ok()),
    "responses_encrypted_reasoning_tool_call": _case(_RESPONSES, _responses([
        {"type": "reasoning", "id": "rs_1", "summary": [{"type": "summary_text", "text": "weigh it"}],
         "encrypted_content": "ENC1"},
        _responses_message("msg_1", "Reading the file."),
        {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "no_such_tool",
         "arguments": '{"path": "a"}'},
    ]), _responses_ok(), conversation_key="input"),
    "responses_image_part_tool_call": _case(_RESPONSES, _responses([
        {"type": "image_generation_call", "id": "ig_1", "result": None},
        {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "no_such_tool",
         "arguments": '{"path": "a"}'},
    ], partial_images=(("ig_1", "aW1hZ2U="),)), _responses_ok(), conversation_key="input"),
}


# The harness --------------------------------------------------------------


class _Endpoint:
    """A mock provider endpoint: records each request body, answers from a script."""

    def __init__(self, replies: tuple[bytes, ...], fallback: bytes):
        self.replies = list(replies)
        self.fallback = fallback
        self.bodies: list[dict] = []

    def respond(self, request: httpx2.Request) -> httpx2.Response:
        self.bodies.append(json.loads(request.content))
        body = self.replies.pop(0) if self.replies else self.fallback
        return httpx2.Response(200, headers={"Content-Type": "text/event-stream"}, content=body)


def _fallback(case: Case) -> bytes:
    if case.provider == "anthropic":
        return _anthropic_ok()
    return _responses_ok() if case.conversation_key == "input" else _chat_ok()


def _normalized(message) -> dict:
    """An SDK message as data, without the random ids the SDK gives each
    message and part."""
    data = message.model_dump(mode="json", exclude={"id"})
    data["parts"] = [{k: v for k, v in part.items() if k != "id"} for part in data["parts"]]
    return data


def _assistant_entries(body: dict, key: str) -> list:
    entries = body.get(key) or []
    if key == "messages":
        return [entry for entry in entries if entry.get("role") == "assistant"]
    # A Responses input carries a reply as separate items.
    return [entry for entry in entries
            if entry.get("role") not in ("system", "user") and entry.get("type") != "function_call_output"]


@dataclass
class Observed:
    records: list[dict]
    live: list[dict]
    resumed: list[dict]
    live_body: list | None
    resumed_body: list | None


def _observe(name: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Observed:
    case = CASES[name]
    endpoint = _Endpoint(case.replies, _fallback(case))
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(endpoint.respond))
    get_provider = model_client.ai.get_provider
    monkeypatch.setattr(model_client.ai, "get_provider",
                        lambda *args, **kwargs: get_provider(*args, **kwargs, client=client))
    convos: list = []

    class KeptConvo(runtime.TurnConvo):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            convos.append(self)

    monkeypatch.setattr(runtime, "TurnConvo", KeptConvo)
    extras = [f"MODEL={case.model}", f"PROVIDER={case.provider}", "API_KEY=fixture",
              "EFFORT=high", "VISION=off", "CONTEXT_SIZE=200000", "OUTPUT_TOKENS_MAX=32000",
              "OUTPUT_TOKENS_RETRY_MAX=0", "REQUEST_LOG=off", "TRANSCRIPT_LOG=off", *case.extras]
    if case.base:
        extras.append(f"BASE_URL={case.base}")
    cfg = from_env(agent_id="replay", session="replay", sources=InvocationSources(extras=tuple(extras)))
    cfg = replace(cfg, session_file=tmp_path / "session.jsonl", project_dir=tmp_path)
    context = ToolContext(cwd=tmp_path)
    monkeypatch.setattr(runtime.T, "STOCK_CONTEXT", context)

    messages: list[dict] = [{"role": "user", "content": "go"}]
    one_turn(cfg, SYSTEM, messages, runtime.Telemetry(None), tool_context=context,
                     trace_override=False, suppress_output=True)
    [convo] = convos
    live_bodies = list(endpoint.bodies)

    memory.persist_messages(cfg.session_file, messages)
    loaded = memory.load_replay_messages(cfg.session_file)
    assert list(loaded) == messages
    first_reply = loaded[:2]
    rebuilt = history_record.history_to_ai_messages(
        SYSTEM, first_reply, provider_id=case.provider, model_id=case.model, vision=convo.vision)

    live_body = resumed_body = None
    if len(live_bodies) > 1:
        # The second request of the turn sent the live message. A resumed
        # session sends the same history, up to the reply that request got.
        prefix = loaded[:-1]

        async def resend():
            await model_client.stream_model_async(
                model_id=case.model, provider_id=case.provider, provider_base_url=case.base,
                provider_api_key="fixture",
                messages=history_record.history_to_ai_messages(
                    SYSTEM, prefix, provider_id=case.provider, model_id=case.model, vision=convo.vision),
                tools=None, max_output_tokens=32000, reasoning_effort="high", on_text=lambda _c: None,
            )

        asyncio.run(resend())
        live_body = _assistant_entries(live_bodies[1], case.conversation_key)
        resumed_body = _assistant_entries(endpoint.bodies[-1], case.conversation_key)
    return Observed(
        records=messages[1:2],
        live=[_normalized(m) for m in convo.ai[2:3]],
        resumed=[_normalized(m) for m in rebuilt[2:3]],
        live_body=live_body,
        resumed_body=resumed_body,
    )


@pytest.mark.parametrize("name", sorted(CASES))
def test_a_reply_is_kept_and_rebuilt_as_the_snapshot_shows(name, monkeypatch, tmp_path):
    observed = _observe(name, monkeypatch, tmp_path).__dict__
    snapshot = SNAPSHOTS / f"{name}.json"
    if os.environ.get("UPDATE_SNAPSHOTS"):
        snapshot.parent.mkdir(exist_ok=True)
        snapshot.write_text(json.dumps(observed, indent=1, sort_keys=True) + "\n")
    assert observed == json.loads(snapshot.read_text())


# Where the message a live turn sends differs today from the one a resumed
# session rebuilds from the record, beyond the usage a live message carries.
KNOWN_DIFFERENCES = {
    "deepseek_cut_off_text": (
        "The live message keeps the reasoning of a reply without tool calls, so the resume nudge's "
        "request sends it; the record rebuilds without it on a route off the OpenAI SDK."),
    "responses_encrypted_reasoning_tool_call": (
        "The live text and function-call parts keep their Responses item ids, so the live request "
        "names them as item references; the record keeps item metadata on reasoning parts only, so a "
        "resumed request sends the text and the call inline."),
    "responses_image_part_tool_call": (
        "The live message keeps the image generation call, its result and the image as parts, and the "
        "live request names the call by item reference; the record keeps none of them."),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_a_live_turn_sends_the_message_a_resumed_session_rebuilds(name, monkeypatch, tmp_path):
    observed = _observe(name, monkeypatch, tmp_path)
    # The live message carries the reply's usage unless js rewrote the reply;
    # a rebuilt one carries none. No request body carries it.
    assert not any(message["usage"] for message in observed.resumed)

    live = [{**message, "usage": None} for message in observed.live]
    same = live == observed.resumed and observed.live_body == observed.resumed_body
    assert same is (name not in KNOWN_DIFFERENCES), KNOWN_DIFFERENCES.get(name, "a new difference")
