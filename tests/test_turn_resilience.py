"""A turn survives what providers do: Retry-After, a silent stream, a reply
cut off by its output cap, and an input the provider cut to fit its window."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from email.utils import format_datetime
from datetime import UTC, datetime, timedelta

import ai
import httpx
import pytest

from js import compaction, model_client, runtime
from js.model_client import ModelStreamResult, ModelToolCall
from js.toolkit import ToolContext
from js.toolkit.registry import build_default_registry
from test_lazy_tool_discovery import _cfg


@pytest.fixture(autouse=True)
def offline_metadata(monkeypatch):
    monkeypatch.setattr(runtime, "_resolve_context_window", lambda *a, **k: 1_000_000)
    monkeypatch.setattr(runtime.model_metadata, "resolve_max_output", lambda *a, **k: None)
    monkeypatch.setattr(runtime, "_backoff", lambda _n: 0)


def _config(tmp_path, runtime_settings: dict | None = None, **fields):
    # A window set for the budget keeps the compaction triggers out of these turns.
    settings = {"compact": {"context_window": 1_000_000}}
    if runtime_settings:
        settings["runtime"] = runtime_settings
    return replace(_cfg(tmp_path, settings), **fields)


def _run(cfg, messages, telemetry=None):
    return runtime.run_turn_async(
        cfg, "system", messages, telemetry or runtime.Telemetry(None),
        tool_registry=build_default_registry().select([]),
        tool_context=ToolContext(cwd=cfg.history_file.parent), suppress_output=True,
    )


class _Events:
    trace_sink = None
    transcript_log = None
    reasoning_factory = None
    display_factory = None

    def __init__(self):
        self.kinds: list[tuple[str, dict]] = []

    def event(self, kind, **fields):
        self.kinds.append((kind, fields))

    def named(self, kind):
        return [fields for k, fields in self.kinds if k == kind]


# --------------------------------------------------------------------------
# A scripted OpenAI-compatible server, spoken to through the real SDK
# --------------------------------------------------------------------------

def _sse(text: str) -> bytes:
    chunks = [
        {"id": "r", "object": "chat.completion.chunk", "created": 1, "model": "m",
         "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}]},
        {"id": "r", "object": "chat.completion.chunk", "created": 1, "model": "m",
         "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        {"id": "r", "object": "chat.completion.chunk", "created": 1, "model": "m", "choices": [],
         "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11}},
    ]
    return b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks) + b"data: [DONE]\n\n"


def _chunk(piece: bytes) -> bytes:
    return f"{len(piece):x}\r\n".encode() + piece + b"\r\n"


class _Server:
    """Answers request N with `script[N]` (the last entry repeats) and records
    when each request arrived."""

    def __init__(self, script):
        self.script = script
        self.arrivals: list[float] = []
        self.server = None
        self.writers: set = set()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}/v1"

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        for writer in self.writers:
            writer.close()
        await self.server.wait_closed()

    async def _serve(self, reader, writer):
        self.writers.add(writer)
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                length = next(int(line.split(b":", 1)[1]) for line in head.split(b"\r\n")
                              if line.lower().startswith(b"content-length:"))
                await reader.readexactly(length)
                self.arrivals.append(time.monotonic())
                step = self.script[min(len(self.arrivals), len(self.script)) - 1]
                if not await step(reader, writer):
                    return
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


def _status(code: int, headers: dict[str, str]):
    async def step(reader, writer):
        body = json.dumps({"error": {"message": f"HTTP {code}", "type": "rate_limit"}}).encode()
        extra = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        writer.write(f"HTTP/1.1 {code} X\r\nContent-Type: application/json\r\n{extra}"
                     f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        await writer.drain()
        return True
    return step


def _answer(text: str, *, keepalive: int = 0, interval: float = 0.0):
    async def step(reader, writer):
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n")
        for _ in range(keepalive):
            writer.write(_chunk(b": keep-alive\n\n"))
            await writer.drain()
            await asyncio.sleep(interval)
        writer.write(_chunk(_sse(text)) + b"0\r\n\r\n")
        await writer.drain()
        return True
    return step


def _silent(*, after_headers: bool):
    async def step(reader, writer):
        if after_headers:
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n")
            writer.write(_chunk(b": opening\n\n"))
            await writer.drain()
        await reader.read()  # until the client gives up and closes
        return False
    return step


def _served_turn(tmp_path, script, runtime_settings=None):
    """Run one turn against a scripted server. Returns (server, messages, error)."""
    messages = [{"role": "user", "content": "go"}]

    async def drive():
        async with _Server(script) as server:
            cfg = _config(tmp_path, runtime_settings, provider_id="openai",
                          provider_api_key="k", provider_base_url=server.url)
            try:
                await asyncio.wait_for(_run(cfg, messages), 30)
            except ai.ProviderAPIError as exc:
                return server, exc
            return server, None

    server, error = asyncio.run(drive())
    return server, messages, error


# --------------------------------------------------------------------------
# Retry-After and the retry budget
# --------------------------------------------------------------------------

@pytest.mark.parametrize(("header", "value", "wait"), [
    ("retry-after", "1", 1.0),
    ("retry-after-ms", "400", 0.4),
])
def test_429_waits_what_retry_after_asks_then_retries(tmp_path, header, value, wait):
    server, messages, error = _served_turn(
        tmp_path, [_status(429, {header: value}), _answer("recovered")])

    assert error is None
    assert messages[-1] == {"role": "assistant", "content": "recovered"}
    assert len(server.arrivals) == 2
    assert server.arrivals[1] - server.arrivals[0] >= wait * 0.9


def test_retry_budget_is_the_setting_and_the_sdk_adds_no_retries(tmp_path):
    server, messages, error = _served_turn(
        tmp_path, [_status(429, {"retry-after": "0"})], {"retry_attempts": 3})

    assert isinstance(error, ai.ProviderAPIError)
    assert len(server.arrivals) == 4  # the first request and three retries
    assert messages[-1]["role"] == "user"


def test_retry_after_longer_than_the_limit_fails_without_waiting(tmp_path):
    started = time.monotonic()
    server, _messages, error = _served_turn(
        tmp_path, [_status(429, {"retry-after": "60"}), _answer("late")],
        {"retry_max_wait_seconds": 5})

    assert isinstance(error, ai.ProviderAPIError)
    assert len(server.arrivals) == 1
    assert time.monotonic() - started < 10


def _rate_limited(headers: dict[str, str]) -> ai.ProviderAPIError:
    response = httpx.Response(429, headers=headers)
    return ai.ProviderRateLimitError(
        "slow down", http_context=ai.errors.HTTPErrorContext(status_code=429, response=response))


def test_retry_after_reads_seconds_milliseconds_and_http_dates():
    assert runtime.retry_after_seconds(_rate_limited({"retry-after": "7"})) == 7
    assert runtime.retry_after_seconds(_rate_limited({"retry-after-ms": "250"})) == 0.25
    # retry-after-ms is the more precise of the two and wins.
    assert runtime.retry_after_seconds(_rate_limited({"retry-after-ms": "250", "retry-after": "7"})) == 0.25
    when = format_datetime(datetime.now(UTC) + timedelta(seconds=30), usegmt=True)
    assert 25 <= runtime.retry_after_seconds(_rate_limited({"retry-after": when})) <= 31
    assert runtime.retry_after_seconds(_rate_limited({})) is None
    assert runtime.retry_after_seconds(_rate_limited({"retry-after": "soon"})) is None


# --------------------------------------------------------------------------
# Stream idle watchdog
# --------------------------------------------------------------------------

@pytest.mark.parametrize("after_headers", [False, True])
def test_a_silent_stream_is_aborted_and_retried(tmp_path, after_headers):
    server, messages, error = _served_turn(
        tmp_path, [_silent(after_headers=after_headers), _answer("recovered")],
        {"stream_idle_seconds": 0.5})

    assert error is None
    assert messages[-1] == {"role": "assistant", "content": "recovered"}
    assert len(server.arrivals) == 2


def test_keep_alive_bytes_hold_off_the_idle_watchdog(tmp_path):
    # 1.2s of keep-alive comments, each 0.15s apart, against a 0.5s limit.
    server, messages, error = _served_turn(
        tmp_path, [_answer("slow but alive", keepalive=8, interval=0.15)],
        {"stream_idle_seconds": 0.5})

    assert error is None
    assert messages[-1] == {"role": "assistant", "content": "slow but alive"}
    assert len(server.arrivals) == 1


def test_a_stream_that_stays_silent_gives_up_after_the_budget(tmp_path):
    server, _messages, error = _served_turn(
        tmp_path, [_silent(after_headers=True)],
        {"stream_idle_seconds": 0.3, "retry_attempts": 1})

    assert isinstance(error, model_client.StreamIdleError)
    assert len(server.arrivals) == 2


# --------------------------------------------------------------------------
# Max-output recovery
# --------------------------------------------------------------------------

def _cut(text: str, calls: list[ModelToolCall] | None = None) -> ModelStreamResult:
    message = ai.assistant_message(text)
    return ModelStreamResult(
        text=text, tool_calls=calls or [], reasoning="",
        usage=ai.types.usage.Usage(input_tokens=10, output_tokens=5),
        finish_reason="incomplete:max_output_tokens", assistant_message=message,
        incomplete_reason="max_output_tokens",
    )


def _done(text: str) -> ModelStreamResult:
    return ModelStreamResult(
        text=text, tool_calls=[], reasoning="",
        usage=ai.types.usage.Usage(input_tokens=10, output_tokens=5),
        finish_reason="stop", assistant_message=ai.assistant_message(text),
    )


def _scripted_model(monkeypatch, replies):
    """stream_model_async answers call N with replies[N] (the last repeats).
    A reply that is an exception is raised. Returns the calls' kwargs."""
    calls: list[dict] = []

    def stub(**kwargs):
        calls.append({**kwargs, "messages": list(kwargs["messages"])})
        reply = replies[min(len(calls), len(replies)) - 1]
        if isinstance(reply, BaseException):
            raise reply
        return reply(len(calls)) if callable(reply) else reply

    monkeypatch.setattr(runtime.model_client, "stream_model_async", stub)
    return calls


def _last_user_text(call: dict) -> str:
    message = call["messages"][-1]
    assert message.role == "user"
    return "".join(part.text for part in message.parts)


def test_cut_off_reply_is_resent_once_with_a_larger_cap(monkeypatch, tmp_path):
    calls = _scripted_model(monkeypatch, [_cut("half"), _done("whole")])
    messages = [{"role": "user", "content": "go"}]

    asyncio.run(_run(_config(tmp_path, max_output_tokens=1000), messages))

    assert [c["max_output_tokens"] for c in calls] == [1000, 64000]
    # The cut reply was not kept: the resend replaced it.
    assert messages == [{"role": "user", "content": "go"}, {"role": "assistant", "content": "whole"}]


def test_escalation_stops_at_the_models_known_output_limit(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime.model_metadata, "resolve_max_output", lambda *a, **k: 32000)
    calls = _scripted_model(monkeypatch, [_cut("half"), _done("whole")])

    asyncio.run(_run(_config(tmp_path, max_output_tokens=1000), [{"role": "user", "content": "go"}]))

    assert [c["max_output_tokens"] for c in calls] == [1000, 32000]


def test_still_cut_off_after_escalation_gets_three_resume_nudges(monkeypatch, tmp_path):
    calls = _scripted_model(monkeypatch, [lambda n: _cut(f"part {n}")])
    events = _Events()
    messages = [{"role": "user", "content": "go"}]

    asyncio.run(_run(_config(tmp_path, max_output_tokens=1000, max_tool_iterations=20), messages, events))

    # One escalated resend, then three nudges at the configured cap, then the turn ends.
    assert [c["max_output_tokens"] for c in calls] == [1000, 64000, 1000, 1000, 1000]
    for call in calls[2:]:
        assert _last_user_text(call) == runtime.MAX_OUTPUT_RESUME_NUDGE
    assert [m["role"] for m in messages] == ["user"] + ["assistant", "user"] * 3 + ["assistant"]
    kept = [m for m in messages if m["role"] == "assistant"]
    assert [m["content"] for m in kept] == ["part 2", "part 3", "part 4", "part 5"]
    assert all(m["incomplete_reason"] == "max_output_tokens" for m in kept)
    assert all(m.get("resume_nudge") for m in messages[2::2])
    assert len(events.named("max_output_resume")) == 3


def test_resume_nudge_that_finishes_ends_the_turn_normally(monkeypatch, tmp_path):
    # The cap is already the model's limit, so there is no larger cap to try.
    monkeypatch.setattr(runtime.model_metadata, "resolve_max_output", lambda *a, **k: 4096)
    calls = _scripted_model(monkeypatch, [_cut("first half"), _done("second half")])
    messages = [{"role": "user", "content": "go"}]

    asyncio.run(_run(_config(tmp_path), messages))

    assert [c["max_output_tokens"] for c in calls] == [4096, 4096]
    assert [m["content"] for m in messages] == [
        "go", "first half", runtime.MAX_OUTPUT_RESUME_NUDGE, "second half"]


def test_rejected_escalation_falls_back_to_resume_nudges(monkeypatch, tmp_path):
    rejected = ai.ProviderBadRequestError("max_tokens is too large", provider="openai")
    calls = _scripted_model(monkeypatch, [_cut("half"), rejected, _cut("half again"), _done("rest")])
    messages = [{"role": "user", "content": "go"}]

    asyncio.run(_run(_config(tmp_path, max_output_tokens=1000), messages))

    assert [c["max_output_tokens"] for c in calls] == [1000, 64000, 1000, 1000]
    assert [m["content"] for m in messages] == [
        "go", "half again", runtime.MAX_OUTPUT_RESUME_NUDGE, "rest"]


def test_cut_off_tool_call_is_dropped_and_the_model_resumes(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime.model_metadata, "resolve_max_output", lambda *a, **k: 4096)
    dangling = [ModelToolCall(id="c1", name="write", arguments='{"file_path": "a.txt", "content": "unfin')]
    calls = _scripted_model(monkeypatch, [_cut("", dangling), _done("smaller pieces")])
    messages = [{"role": "user", "content": "go"}]

    asyncio.run(_run(_config(tmp_path), messages))

    assert len(calls) == 2
    assert "tool_calls" not in messages[1]
    assert messages[2]["content"] == runtime.MAX_OUTPUT_RESUME_NUDGE
    assert messages[-1] == {"role": "assistant", "content": "smaller pieces"}
    assert not (tmp_path / "a.txt").exists()


def test_resume_nudges_are_a_setting(monkeypatch, tmp_path):
    calls = _scripted_model(monkeypatch, [_cut("half")])
    messages = [{"role": "user", "content": "go"}]

    asyncio.run(_run(_config(tmp_path, {"max_output_escalation": 0, "max_output_resumes": 1},
                             max_output_tokens=1000), messages))

    assert [c["max_output_tokens"] for c in calls] == [1000, 1000]
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant"]


def test_sdk_length_finish_marks_the_reply_cut_off(monkeypatch):
    events = [
        ai.types.events.StreamStart(),
        ai.types.events.TextStart(block_id="t"),
        ai.types.events.TextDelta(chunk="half", block_id="t"),
        ai.types.events.TextEnd(block_id="t"),
        ai.types.events.StreamEnd(finish_reason="length"),
    ]

    async def generate():
        for event in events:
            yield event

    monkeypatch.setattr(model_client, "_open_stream", lambda **_k: ai.models.Stream(generate()))
    result = model_client.stream_model(
        model_id="t", provider_id="openai", provider_base_url="http://127.0.0.1:1/v1",
        provider_api_key="k", messages=[ai.user_message("hi")], tools=None,
        max_output_tokens=16, reasoning_effort=None, on_text=lambda _t: None,
    )
    assert result.incomplete_reason == "max_output_tokens"
    assert result.finish_reason == "incomplete:max_output_tokens"


# --------------------------------------------------------------------------
# Silent overflow
# --------------------------------------------------------------------------

def _answer_with_usage(text: str, input_tokens: int, cache_read: int = 0) -> ModelStreamResult:
    return ModelStreamResult(
        text=text, tool_calls=[], reasoning="",
        usage=ai.types.usage.Usage(input_tokens=input_tokens, output_tokens=5,
                                   cache_read_tokens=cache_read),
        finish_reason="stop", assistant_message=ai.assistant_message(text),
    )


def _history_with_old_results() -> list[dict]:
    messages: list[dict] = []
    for i in range(30):
        messages.extend([
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": f"c{i}", "type": "function", "function": {"name": "read", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": f"c{i}", "name": "read", "content": "x" * 1000},
        ])
    messages.append({"role": "user", "content": "continue"})
    return messages


def test_a_stop_whose_input_exceeds_the_window_is_treated_as_overflow(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime, "_resolve_context_window", lambda *a, **k: 20_000)
    # The SDK counts cache reads inside input_tokens: 24k with 20k of them cached.
    calls = _scripted_model(monkeypatch, [
        _answer_with_usage("answer from a cut input", 24_000, cache_read=20_000),
        _answer_with_usage("real answer", 12_000),
    ])
    events = _Events()
    messages = _history_with_old_results()

    asyncio.run(_run(_config(tmp_path), messages, events))

    assert len(calls) == 2
    assert messages[-1] == {"role": "assistant", "content": "real answer"}
    assert any(m["content"] == compaction.MICROCOMPACT_CLEARED_MESSAGE for m in messages if m["role"] == "tool")
    assert events.named("context_overflow_silent")[0]["prompt_tokens"] == 24_000


def test_a_stop_inside_the_window_is_kept(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime, "_resolve_context_window", lambda *a, **k: 20_000)
    calls = _scripted_model(monkeypatch, [_answer_with_usage("fits", 19_000, cache_read=15_000)])
    messages = _history_with_old_results()

    asyncio.run(_run(_config(tmp_path), messages))

    assert len(calls) == 1
    assert messages[-1] == {"role": "assistant", "content": "fits"}
    assert all(m["content"] == "x" * 1000 for m in messages if m["role"] == "tool")
