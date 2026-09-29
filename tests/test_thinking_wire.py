"""Thinking on the Anthropic Messages wire, and signed thinking across turns.

Every provider on the anthropic SDK takes the reasoning setting as thinking.
The HTTP body is captured from the SDK's own request builder through a mock
transport, so these pin what reaches the endpoint.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import ai
import httpx2
import pytest

from js import logins, memory, model_client, runtime
from js.config import from_env
from js.logins import Login
from js.sampling import Sampling
from js.toolkit import ToolContext


_THOUGHT = "weigh the two options"
_SIGNATURE = "SIG-opaque-1"


def _sse(events: list[dict]) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def _anthropic_events(model: str, *, thought: str = _THOUGHT, signature: str = _SIGNATURE,
                      text: str = "ok", tool: tuple[str, str] | None = None) -> list[dict]:
    events: list[dict] = [
        {"type": "message_start", "message": {
            "id": "msg_1", "type": "message", "role": "assistant", "model": model, "content": [],
            "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 5, "output_tokens": 1}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": thought}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": signature}},
        {"type": "content_block_stop", "index": 0},
    ]
    if tool is None:
        events += [
            {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": text}},
            {"type": "content_block_stop", "index": 1},
        ]
        stop = "end_turn"
    else:
        call_id, name = tool
        events += [
            {"type": "content_block_start", "index": 1,
             "content_block": {"type": "tool_use", "id": call_id, "name": name, "input": {}}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "{}"}},
            {"type": "content_block_stop", "index": 1},
        ]
        stop = "tool_use"
    events += [
        {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None}, "usage": {"output_tokens": 5}},
        {"type": "message_stop"},
    ]
    return events


class _Wire:
    """A mock Anthropic endpoint: records each request body, answers from a script."""

    def __init__(self, model: str, script: list[dict] | None = None):
        self.model = model
        self.script = list(script or [])
        self.bodies: list[dict] = []

    refuse: frozenset[int] = frozenset()

    def respond(self, request: httpx2.Request) -> httpx2.Response:
        if len(self.bodies) in self.refuse:
            # Anthropic's 400 for a thinking block bound to another conversation.
            self.bodies.append(json.loads(request.content))
            return httpx2.Response(400, json={"type": "error", "error": {
                "type": "invalid_request_error",
                "message": "messages.1.content.0: Invalid `signature` in `thinking` block. "
                           "The block is bound to a different conversation."}})
        self.bodies.append(json.loads(request.content))
        kwargs = self.script.pop(0) if self.script else {}
        return httpx2.Response(200, headers={"Content-Type": "text/event-stream"},
                               content=_sse(_anthropic_events(self.model, **kwargs)))

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(self.respond))
        get_provider = model_client.ai.get_provider

        def with_transport(*args, **kwargs):
            return get_provider(*args, **kwargs, client=client)

        monkeypatch.setattr(model_client.ai, "get_provider", with_transport)


def _send(monkeypatch, *, provider: str, model: str, effort: str | None,
          max_out: int | None, base: str | None = None,
          extra: dict | None = None, sampling: Sampling | None = None) -> dict:
    wire = _Wire(model)
    wire.install(monkeypatch)

    async def drive():
        return await model_client.stream_model_async(
            model_id=model, provider_id=provider, provider_base_url=base,
            provider_api_key="fixture", messages=[ai.user_message("hi")], tools=None,
            max_output_tokens=max_out, reasoning_effort=effort, on_text=lambda _c: None,
            provider_extra=extra, sampling=sampling,
        )

    asyncio.run(drive())
    assert len(wire.bodies) == 1
    return wire.bodies[0]


def _thinking_fields(body: dict) -> dict:
    return {k: body[k] for k in ("thinking", "output_config", "max_tokens") if k in body}


@pytest.mark.parametrize(("provider", "base", "model"), [
    ("anthropic", None, "claude-sonnet-4-5"),
    ("anthropic-custom", "http://claude.test", "claude-sonnet-4-5"),
    ("opencode-go-anthropic", "https://opencode.ai/zen/go", "qwen3.7-plus"),
    ("minimax", "https://api.minimax.io/anthropic/v1", "MiniMax-M2"),
])
def test_every_anthropic_sdk_provider_sends_a_thinking_budget(monkeypatch, provider, base, model):
    body = _send(monkeypatch, provider=provider, base=base, model=model, effort="high", max_out=32000)

    assert _thinking_fields(body) == {
        "thinking": {"type": "enabled", "budget_tokens": 16384},
        "max_tokens": 32000,
    }


def test_a_saved_login_on_the_anthropic_sdk_sends_a_thinking_budget(monkeypatch, tmp_path):
    monkeypatch.setattr(logins, "_CONFIG_DIR_OVERRIDE", tmp_path / "login-store")
    logins.save_login(Login(
        provider_id="myclaude", sdk_provider_id="anthropic",
        provider_base_url="http://claude.test", provider_api_key="x",
    ))

    body = _send(monkeypatch, provider="myclaude", base="http://claude.test", model="some-model",
                 effort="low", max_out=8000)

    assert _thinking_fields(body) == {
        "thinking": {"type": "enabled", "budget_tokens": 2048},
        "max_tokens": 8000,
    }


@pytest.mark.parametrize(("effort", "budget"), [
    ("minimal", 1024), ("low", 2048), ("medium", 8192), ("high", 16384), ("xhigh", 24576), ("max", 32000),
])
def test_the_budget_follows_the_reasoning_setting(monkeypatch, effort, budget):
    body = _send(monkeypatch, provider="anthropic", model="claude-haiku-4-5", effort=effort, max_out=64000)

    assert body["thinking"] == {"type": "enabled", "budget_tokens": budget}


def test_the_budget_leaves_room_for_the_answer_under_the_output_cap(monkeypatch):
    body = _send(monkeypatch, provider="anthropic", model="claude-haiku-4-5", effort="high", max_out=4096)

    assert _thinking_fields(body) == {
        "thinking": {"type": "enabled", "budget_tokens": 3072},
        "max_tokens": 4096,
    }


def test_no_thinking_when_the_output_cap_has_no_room_for_a_budget(monkeypatch):
    body = _send(monkeypatch, provider="anthropic", model="claude-haiku-4-5", effort="high", max_out=1500)

    assert "thinking" not in body
    assert body["max_tokens"] == 1500


def test_an_unknown_output_cap_is_raised_above_the_budget(monkeypatch):
    body = _send(monkeypatch, provider="minimax", base="https://api.minimax.io/anthropic/v1",
                 model="MiniMax-M2", effort="medium", max_out=None)

    assert body["thinking"] == {"type": "enabled", "budget_tokens": 8192}
    assert body["max_tokens"] > 8192


@pytest.mark.parametrize("effort", [None, "none"])
def test_a_budget_model_gets_no_thinking_when_reasoning_is_unset_or_none(monkeypatch, effort):
    body = _send(monkeypatch, provider="anthropic", model="claude-sonnet-4-5", effort=effort, max_out=32000)

    assert "thinking" not in body
    assert "output_config" not in body


@pytest.mark.parametrize(("model", "effort", "sent"), [
    ("claude-opus-4-8", "high", "high"),
    ("claude-opus-4-8", "xhigh", "xhigh"),
    ("claude-opus-4-8", "minimal", "low"),
    ("claude-sonnet-4-6", "xhigh", "high"),
    ("claude-opus-5-5", "max", "max"),
    ("claude-fable-5-1", "medium", "medium"),
])
def test_an_adaptive_model_gets_adaptive_thinking_and_an_effort(monkeypatch, model, effort, sent):
    body = _send(monkeypatch, provider="anthropic", model=model, effort=effort, max_out=64000)

    assert _thinking_fields(body) == {
        "thinking": {"type": "adaptive", "display": "summarized"},
        "output_config": {"effort": sent},
        "max_tokens": 64000,
    }


def test_none_disables_thinking_where_the_model_accepts_it(monkeypatch):
    body = _send(monkeypatch, provider="anthropic", model="claude-opus-4-8", effort="none", max_out=64000)

    assert body["thinking"] == {"type": "disabled"}
    assert "output_config" not in body


@pytest.mark.parametrize("model", ["claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1"])
def test_none_is_the_lowest_effort_on_a_model_that_always_thinks(monkeypatch, model):
    body = _send(monkeypatch, provider="anthropic", model=model, effort="none", max_out=64000)

    assert "thinking" not in body
    assert body["output_config"] == {"effort": "low"}


def test_thinking_in_provider_extra_wins(monkeypatch):
    mine = {"type": "enabled", "budget_tokens": 5000}
    body = _send(monkeypatch, provider="anthropic", model="claude-sonnet-4-5", effort="high",
                 max_out=32000, extra={"thinking": mine})

    assert body["thinking"] == mine


def test_budget_thinking_sends_no_temperature_or_top_k(monkeypatch):
    sampling = Sampling(temperature=0.2, top_k=40)
    body = _send(monkeypatch, provider="anthropic", model="claude-sonnet-4-5", effort="high",
                 max_out=32000, sampling=sampling)

    assert body["thinking"]["type"] == "enabled"
    assert "temperature" not in body and "top_k" not in body


# --- signatures -------------------------------------------------------------


@pytest.fixture
def claude(monkeypatch, tmp_path):
    cfg = from_env(
        agent_id="thinking",
        session="thinking",
        extras=[
            "model.id=claude-sonnet-4-5",
            "provider.id=anthropic",
            "provider.api_key=fixture",
            "model.reasoning_effort=high",
            "model.vision=off",
            "model.context_window=200000",
            "model.max_output_tokens=32000",
            "runtime.debug_autolog=off",
            "runtime.transcript_log=off",
        ],
    )
    prompts = tmp_path / ".js" / "agents" / "thinking"
    prompts.mkdir(parents=True)
    (prompts / "01-prompt.md").write_text("SYSTEM\n")
    cfg = replace(cfg, prompts_dir=prompts, project_dir=tmp_path)
    context = ToolContext(cwd=tmp_path)
    monkeypatch.setattr(runtime.T, "STOCK_CONTEXT", context)
    wire = _Wire("claude-sonnet-4-5")
    wire.install(monkeypatch)
    return cfg, wire, context


def _turn(cfg, messages, context, **kwargs):
    runtime.run_turn(cfg, "SYSTEM", messages, runtime.Telemetry(None),
                     tool_context=context, trace_override=False, suppress_output=True, **kwargs)


def _assistant_blocks(body: dict) -> list[list[dict]]:
    return [m["content"] for m in body["messages"] if m["role"] == "assistant"]


_SIGNED_BLOCK = {"type": "thinking", "thinking": _THOUGHT, "signature": _SIGNATURE}


def test_the_history_record_keeps_the_signature_and_its_origin(claude):
    cfg, _wire, context = claude
    messages = [{"role": "user", "content": "first"}]
    _turn(cfg, messages, context)

    record = messages[-1]
    assert record["reasoning_parts"] == [
        {"text": _THOUGHT, "provider_metadata": {"anthropic": {"signature": _SIGNATURE}}},
    ]
    assert record["reasoning_from"] == {"provider": "anthropic", "model": "claude-sonnet-4-5"}


def test_the_next_turn_replays_the_signed_thinking_block(claude):
    cfg, wire, context = claude
    messages = [{"role": "user", "content": "first"}]
    _turn(cfg, messages, context)
    messages.append({"role": "user", "content": "next"})
    _turn(cfg, messages, context)

    assert _assistant_blocks(wire.bodies[1]) == [[_SIGNED_BLOCK, {"type": "text", "text": "ok"}]]


def test_a_resumed_session_replays_the_signed_thinking_block(claude):
    cfg, wire, context = claude
    messages = [{"role": "user", "content": "first"}]
    _turn(cfg, messages, context)
    memory.persist_messages(cfg.session_file, messages,
                            memory.stamp_for("claude-sonnet-4-5", "anthropic", "high"))

    resumed = memory.load_replay_messages(cfg.session_file)
    resumed.append({"role": "user", "content": "next"})
    _turn(cfg, resumed, context)

    assert _assistant_blocks(wire.bodies[1]) == [[_SIGNED_BLOCK, {"type": "text", "text": "ok"}]]


def test_signed_thinking_stays_with_its_tool_call_inside_one_turn(claude):
    cfg, wire, context = claude
    wire.script = [{"tool": ("toolu_1", "no_such_tool")}, {}]
    messages = [{"role": "user", "content": "go"}]
    _turn(cfg, messages, context)

    blocks = _assistant_blocks(wire.bodies[1])[0]
    assert blocks[0] == _SIGNED_BLOCK
    assert blocks[1]["type"] == "tool_use"


def test_signed_thinking_is_not_sent_to_another_model(claude):
    cfg, wire, context = claude
    messages = [{"role": "user", "content": "first"}]
    _turn(cfg, messages, context)
    messages.append({"role": "user", "content": "next"})
    _turn(cfg, messages, context, model_override="claude-haiku-4-5")

    assert _assistant_blocks(wire.bodies[1]) == [[{"type": "text", "text": "ok"}]]


def test_replay_puts_each_signed_part_back_between_its_tool_calls():
    calls = [
        {"id": f"call_{n}", "type": "function", "function": {"name": "read", "arguments": "{}"}}
        for n in (1, 2)
    ]
    record = {
        "role": "assistant", "content": "", "tool_calls": calls,
        "reasoning_content": "ab",
        "reasoning_parts": [
            {"text": "a", "provider_metadata": {"anthropic": {"signature": "S1"}}},
            {"text": "b", "provider_metadata": {"anthropic": {"signature": "S2"}}, "after_calls": 1},
        ],
        "reasoning_from": {"provider": "anthropic", "model": "claude-opus-5-5"},
    }
    message = model_client.history_to_ai_messages(
        "", [record], provider_id="anthropic", model_id="claude-opus-5-5",
    )[0]

    shape = [
        (p.text, p.provider_metadata["anthropic"]["signature"]) if p.kind == "reasoning" else p.tool_call_id
        for p in message.parts
    ]
    assert shape == [("a", "S1"), "call_1", ("b", "S2"), "call_2"]


def test_the_reduced_history_view_drops_signed_reasoning_with_the_text():
    record = {
        "role": "assistant", "content": "ok", "reasoning_content": _THOUGHT,
        "reasoning_parts": [{"text": _THOUGHT, "provider_metadata": {"anthropic": {"signature": _SIGNATURE}}}],
        "reasoning_from": {"provider": "anthropic", "model": "claude-sonnet-4-5"},
    }

    assert memory._strip_orphan_reasoning([record]) == [{"role": "assistant", "content": "ok"}]


# --- edits to earlier history -------------------------------------------------
#
# A thinking signature is bound to the history before it. After js edits an
# earlier message, the signed thinking after the edit is not replayed.


def _signed_call(n: int) -> dict:
    return {
        "role": "assistant", "content": "",
        "tool_calls": [{"id": f"call_{n}", "type": "function", "function": {"name": "read", "arguments": "{}"}}],
        "reasoning_content": f"t{n}",
        "reasoning_parts": [{"text": f"t{n}", "provider_metadata": {"anthropic": {"signature": f"S{n}"}}}],
        "reasoning_from": {"provider": "anthropic", "model": "claude-opus-5-5"},
    }


def _result(n: int) -> dict:
    return {"role": "tool", "tool_call_id": f"call_{n}", "name": "read", "content": f"body {n} " * 100}


def _three_tool_rounds() -> list[dict]:
    return [{"role": "user", "content": "go"},
            _signed_call(1), _result(1), _signed_call(2), _result(2), _signed_call(3), _result(3)]


def _wire_signatures(monkeypatch, messages: list[dict]) -> list[list[str]]:
    """The thinking signatures of each assistant turn as the Anthropic body carries them."""
    wire = _Wire("claude-opus-5-5")
    wire.install(monkeypatch)

    async def drive():
        return await model_client.stream_model_async(
            model_id="claude-opus-5-5", provider_id="anthropic", provider_base_url=None,
            provider_api_key="fixture",
            messages=model_client.history_to_ai_messages(
                "SYSTEM", messages, provider_id="anthropic", model_id="claude-opus-5-5"),
            tools=None, max_output_tokens=4096, reasoning_effort="high", on_text=lambda _c: None,
        )

    asyncio.run(drive())
    return [[b["signature"] for b in blocks if b["type"] == "thinking"]
            for blocks in _assistant_blocks(wire.bodies[0])]


def test_signed_thinking_after_a_cleared_tool_result_is_not_replayed(monkeypatch):
    from js import compaction

    messages = _three_tool_rounds()
    cleared, _ = compaction.microcompact(messages, keep_recent=1)

    assert cleared == 2
    assert _wire_signatures(monkeypatch, messages) == [["S1"], [], []]


def test_a_keep_tail_compaction_drops_the_signed_thinking_it_keeps(monkeypatch, tmp_path):
    from js import compaction
    from test_config_compaction_layers import _compact_test_cfg

    cfg = _compact_test_cfg(tmp_path, {"flight_log_dir": str(tmp_path / "flights"), "tail_tokens": 1})

    async def summarize(*_a, **_kw):
        return "summary"

    monkeypatch.setattr(compaction, "summarize", summarize)
    messages = _three_tool_rounds()
    messages[0] = {"role": "user", "content": "go " * 2000}
    asyncio.run(compaction.compact_now(cfg, "SYSTEM", messages, forced=True, preserve_from=3,
                                        context=ToolContext(cwd=tmp_path)))

    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "assistant", "tool"]
    assert _wire_signatures(monkeypatch, messages) == [[], []]
    resumed = memory.load_replay_messages(cfg.session_file)
    assert resumed == messages


def test_signed_thinking_after_a_user_message_that_lost_its_attachment_is_not_replayed():
    from js import cli

    runtime_message = {"role": "user", "content": [ai.types.messages.TextPart(text="see"),
                                                   ai.types.messages.FilePart(data=b"png", media_type="image/png")]}
    history_message = {"role": "user", "content": "see"}
    messages = [runtime_message, _signed_call(1), _result(1)]
    cli._replace_runtime_user_message(messages, runtime_message, history_message, 0)

    assert messages[0] == history_message
    assert "reasoning_parts" not in messages[1]

    plain = {"role": "user", "content": "see"}
    messages = [plain, _signed_call(1), _result(1)]
    cli._replace_runtime_user_message(messages, plain, dict(plain), 0)

    assert "reasoning_parts" in messages[1]


def test_a_refused_signature_is_retried_once_without_signed_thinking(claude):
    cfg, wire, context = claude
    wire.refuse = frozenset({1})
    messages = [{"role": "user", "content": "first"}]
    _turn(cfg, messages, context)
    messages.append({"role": "user", "content": "next"})
    _turn(cfg, messages, context)

    assert len(wire.bodies) == 3
    assert _assistant_blocks(wire.bodies[1]) == [[_SIGNED_BLOCK, {"type": "text", "text": "ok"}]]
    assert _assistant_blocks(wire.bodies[2]) == [[{"type": "text", "text": "ok"}]]
    assert "reasoning_parts" not in messages[1]
    assert messages[-1]["reasoning_parts"]


def test_a_second_refusal_in_one_request_is_raised(claude):
    cfg, wire, context = claude
    wire.refuse = frozenset({1, 2})
    messages = [{"role": "user", "content": "first"}]
    _turn(cfg, messages, context)
    messages.append({"role": "user", "content": "next"})

    with pytest.raises(ai.ProviderBadRequestError):
        _turn(cfg, messages, context)
    assert len(wire.bodies) == 3


def _signed(text: str, sig: str) -> ai.types.messages.ReasoningPart:
    return ai.thinking(text, provider_metadata={"anthropic": {"signature": sig}})


def _call(call_id: str) -> ai.types.messages.ToolCallPart:
    return ai.types.messages.ToolCallPart(tool_call_id=call_id, tool_name="read", tool_args="{}")


def test_a_normalized_batch_keeps_each_signed_part_between_its_calls():
    message = ai.assistant_message(_signed("a", "S1"), _call("c1"), _signed("b", "S2"), _call("c2"))
    calls = [runtime._PendingToolCall(id=c, name="read", arg_chunks=["{}"]) for c in ("c1", "c2")]
    normalized = runtime._assistant_message_with_tool_calls(message, calls)

    shape = [p.provider_metadata["anthropic"]["signature"] if p.kind == "reasoning" else p.tool_call_id
             for p in normalized.parts]
    assert shape == ["S1", "c1", "S2", "c2"]
    assert [p.get("after_calls") for p in model_client.signed_reasoning_parts(normalized)] == [None, 1]


def test_a_signed_part_after_a_dropped_call_is_left_out():
    message = ai.assistant_message(_signed("a", "S1"), _call("c1"), _call("dup"), _signed("b", "S2"), _call("c2"))
    calls = [runtime._PendingToolCall(id=c, name="read", arg_chunks=["{}"]) for c in ("c1", "c2")]
    normalized = runtime._assistant_message_with_tool_calls(message, calls, diagnostic_suffix="capped")

    shape = [p.provider_metadata["anthropic"]["signature"] if p.kind == "reasoning"
             else p.tool_call_id if p.kind == "tool_call" else p.text
             for p in normalized.parts]
    assert shape == ["S1", "capped", "c1", "c2"]


def test_text_between_signed_thinking_blocks_replays_in_the_order_it_came(monkeypatch):
    live = ai.assistant_message(
        ai.thinking("a", provider_metadata={"anthropic": {"signature": "S1"}}),
        ai.types.messages.TextPart(text="checking"),
        ai.thinking("b", provider_metadata={"anthropic": {"signature": "S2"}}),
        ai.types.messages.ToolCallPart(tool_call_id="call_1", tool_name="read", tool_args="{}"),
    )
    record = {**_signed_call(1), "content": "checking", "reasoning_parts": model_client.signed_reasoning_parts(live)}
    wire = _Wire("claude-opus-5-5")
    wire.install(monkeypatch)
    asyncio.run(model_client.stream_model_async(
        model_id="claude-opus-5-5", provider_id="anthropic", provider_base_url=None,
        provider_api_key="fixture",
        messages=model_client.history_to_ai_messages(
            "SYSTEM", [{"role": "user", "content": "go"}, record, _result(1)],
            provider_id="anthropic", model_id="claude-opus-5-5"),
        tools=None, max_output_tokens=4096, reasoning_effort="high", on_text=lambda _c: None,
    ))

    assert [b["type"] for b in _assistant_blocks(wire.bodies[0])[0]] == ["thinking", "text", "thinking", "tool_use"]


def test_a_drop_of_signed_reasoning_on_a_final_answer_survives_resume(tmp_path):
    session = tmp_path / "session.jsonl"
    answer = {**_signed_call(1), "content": "done"}
    del answer["tool_calls"]
    messages = [{"role": "user", "content": "go"}, answer, {"role": "user", "content": "more"},
                _signed_call(2), _result(2)]
    memory.persist_messages(session, messages)

    assert memory.drop_signed_reasoning(messages) == 2
    memory.persist_messages(session, messages)

    assert memory.load_replay_messages(session) == messages
