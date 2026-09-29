"""The `ui.net` channel: connection lines, failures, and the byte counter.

The streaming tests run a real OpenAI-compatible SSE server on 127.0.0.1 and
drive `stream_model_async` through the actual SDK and transport.
"""

from __future__ import annotations

import asyncio
import json
import re
import socket

import dataclasses

import ai
import httpx2
import pytest
from test_status_bar import _cfg

from js import model_client, runtime, stream_transport
from js.model_client import ModelStreamResult
from js.toolkit.core import ToolContext, TurnStatus


@pytest.fixture
def sink():
    lines: list[str] = []
    level = {"value": 2}
    stream_transport.install_sink(stream_transport.NetSink(
        level=lambda: level["value"], emit=lines.append,
    ))
    try:
        yield lines, level
    finally:
        stream_transport.install_sink(None)


def _plain(line: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", line)


def _sse_body() -> bytes:
    chunks = [
        {"id": "t", "object": "chat.completion.chunk", "created": 1, "model": "t",
         "choices": [{"index": 0, "delta": {"role": "assistant", "content": "OK"}, "finish_reason": None}]},
        {"id": "t", "object": "chat.completion.chunk", "created": 1, "model": "t",
         "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]
    body = b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks)
    return body + b"data: [DONE]\n\n"


async def _serve_sse(reader, writer):
    head = await reader.readuntil(b"\r\n\r\n")
    length = next(int(line.split(b":", 1)[1]) for line in head.split(b"\r\n")
                  if line.lower().startswith(b"content-length:"))
    await reader.readexactly(length)
    writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                 b"Transfer-Encoding: chunked\r\n\r\n")
    # Keep-alive comments arrive before the first token: the bar counts these.
    for _ in range(3):
        piece = b": keep-alive " + b"x" * 200 + b"\n\n"
        writer.write(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
        await writer.drain()
        await asyncio.sleep(0.02)
    body = _sse_body()
    writer.write(f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n\r\n")
    await writer.drain()
    writer.close()


def _stream_once(status: TurnStatus, events: list):
    async def drive():
        server = await asyncio.start_server(_serve_sse, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            with stream_transport.net_role(status=status):
                def on_text(chunk):
                    events.append(("text", chunk, status.net_bytes))

                return await asyncio.wait_for(model_client.stream_model_async(
                    model_id="t", provider_id="openai",
                    provider_base_url=f"http://127.0.0.1:{port}/v1", provider_api_key="k",
                    messages=[ai.user_message("hi")], tools=None, max_output_tokens=16,
                    reasoning_effort=None, on_text=on_text,
                ), 10)
        finally:
            server.close()
            await server.wait_closed()

    return asyncio.run(drive())


class _RecordingStatus(TurnStatus):
    def add_bytes(self, n: int) -> None:
        super().add_bytes(n)
        self.__dict__.setdefault("history", []).append(self.net_bytes)


def test_level_2_prints_connecting_then_connected_before_any_token(sink):
    lines, _level = sink
    events: list = []
    status = _RecordingStatus()
    real_emit = lines.append
    stream_transport.install_sink(stream_transport.NetSink(
        level=lambda: 2, emit=lambda line: (real_emit(line), events.append(("net", line))),
    ))

    result = _stream_once(status, events)

    assert result.text == "OK"
    kinds = [kind for kind, *_ in events]
    assert kinds[:2] == ["net", "net"], events
    assert kinds.index("text") >= 2
    assert re.search(r"\d+ms", _plain(events[1][1]))
    assert "127.0.0.1" in _plain(events[0][1])


def test_byte_counter_climbs_before_the_first_token(sink):
    events: list = []
    status = _RecordingStatus()

    _stream_once(status, events)

    history = status.__dict__["history"]
    before_token = [n for n in history if n > 0]
    assert len(before_token) >= 2
    assert before_token == sorted(before_token)
    first_text = next(e for e in events if e[0] == "text")
    assert first_text[2] > 0            # bytes were on the bar when the token landed


def test_level_1_prints_no_connection_lines(sink):
    lines, level = sink
    level["value"] = 1

    _stream_once(TurnStatus(), [])

    assert lines == []


def _dns_failure() -> Exception:
    try:
        try:
            raise socket.gaierror(-2, "Name or service not known")
        except socket.gaierror as exc:
            request = httpx2.Request("POST", "https://nowhere.invalid/v1/chat/completions")
            raise httpx2.ConnectError(str(exc), request=request) from exc
    except httpx2.ConnectError as exc:
        err = ai.ProviderAPIError("Connection error.", is_retryable=True)
        err.__cause__ = exc
        return err


def _fail_stream(monkeypatch, exc: Exception) -> None:
    class _FakeProvider:
        base_url = "https://nowhere.invalid/v1"

        async def aclose(self) -> None:
            pass

    class _FakeModel:
        provider = _FakeProvider()

    async def fake_stream_async(**_kwargs) -> ModelStreamResult:
        raise exc

    monkeypatch.setattr(model_client, "resolve_model", lambda *a, **k: _FakeModel())
    monkeypatch.setattr(model_client, "_stream_async", fake_stream_async)
    with pytest.raises(type(exc)):
        model_client.stream_model(
            model_id="m", provider_id="openai", provider_base_url=None,
            provider_api_key="k", messages=[ai.user_message("hi")], tools=None,
            max_output_tokens=16, reasoning_effort=None, on_text=lambda _c: None,
        )


def test_dns_failure_prints_one_line_at_level_1(monkeypatch, sink):
    lines, level = sink
    level["value"] = 1

    _fail_stream(monkeypatch, _dns_failure())

    assert len(lines) == 1
    assert "nowhere.invalid" in _plain(lines[0])


def test_dns_failure_prints_nothing_at_level_0(monkeypatch, sink):
    lines, level = sink
    level["value"] = 0

    _fail_stream(monkeypatch, _dns_failure())

    assert lines == []


def test_no_sink_no_channel(monkeypatch):
    stream_transport.install_sink(None)
    assert stream_transport.begin_call("https://x/v1", "m") is None
    assert stream_transport.net_level() is None
    _fail_stream(monkeypatch, _dns_failure())


def test_http_status_failure_names_the_status():
    err = ai.ProviderAPIError("Error code: 429", code="insufficient_quota")
    err.http_context = type("Ctx", (), {"status_code": 429})()
    text = _plain(stream_transport.describe_failure(err))
    assert "429" in text and "insufficient_quota" in text


def test_roles_label_subagent_and_compaction_lines(sink):
    lines, _level = sink
    with stream_transport.net_role("Subagent 2", agent="summarizer"):
        call = stream_transport.begin_call("https://api.deepseek.com", "deepseek/deepseek-v4-flash")
    with stream_transport.net_role("Compacting"):
        stream_transport.begin_call("https://api.deepseek.com", "deepseek/deepseek-v4-flash")
    call.first_token()

    plain = [_plain(line) for line in lines]
    assert "Subagent 2" in plain[0] and "summarizer" in plain[0] and "api.deepseek.com" in plain[0]
    assert "deepseek/deepseek-v4-flash" in plain[1] and "api.deepseek.com" in plain[1]
    assert "Subagent 2" in plain[2] and re.search(r"\d+ms", plain[2])


def _naming(lines: list[str], host: str) -> list[str]:
    """The lines about `host`: a turn may also refresh the models.dev catalog."""
    return [_plain(line) for line in lines if host in _plain(line)]


def _turn(tmp_path, context: ToolContext, *, cfg=None) -> None:
    asyncio.run(runtime.run_turn_async(
        cfg or _cfg(tmp_path), "sys", [{"role": "user", "content": "go"}],
        runtime.Telemetry(debug_log=None), tool_context=context, suppress_output=True,
    ))


def _flaky_model(monkeypatch, failures: list[Exception]) -> list[int]:
    """`_stream_async` raises each of `failures` in turn, then answers."""
    calls: list[int] = []

    class _FakeProvider:
        base_url = "https://nowhere.invalid/v1"

        async def aclose(self) -> None:
            pass

    class _FakeModel:
        provider = _FakeProvider()

    async def fake_stream_async(*, on_text, **_kwargs) -> ModelStreamResult:
        calls.append(1)
        if failures:
            raise failures.pop(0)
        on_text("OK")
        return ModelStreamResult(
            text="OK", tool_calls=[], reasoning="",
            usage=ai.types.usage.Usage(input_tokens=1, output_tokens=1),
            finish_reason="stop", assistant_message=ai.assistant_message("OK"),
        )

    monkeypatch.setattr(model_client, "resolve_model", lambda *a, **k: _FakeModel())
    monkeypatch.setattr(model_client, "_stream_async", fake_stream_async)
    monkeypatch.setattr(runtime, "_backoff", lambda _n: 0)
    return calls


def test_turn_that_gives_up_on_dns_prints_one_line_at_level_1(monkeypatch, sink, tmp_path):
    lines, level = sink
    level["value"] = 1
    calls = _flaky_model(monkeypatch, [_dns_failure() for _ in range(5)])

    with pytest.raises(ai.ProviderAPIError):
        _turn(tmp_path, ToolContext(cwd=tmp_path))

    assert len(calls) > 1                       # the runtime retried
    assert len(_naming(lines, "nowhere.invalid")) == 1


def test_turn_retries_show_at_level_3_and_end_with_the_failure(monkeypatch, sink, tmp_path):
    lines, level = sink
    level["value"] = 3
    calls = _flaky_model(monkeypatch, [_dns_failure() for _ in range(5)])

    with pytest.raises(ai.ProviderAPIError):
        _turn(tmp_path, ToolContext(cwd=tmp_path))

    # Each call says Connecting (with the URL); each failure after it names the
    # host alone: one per retry, then the one the runtime gave up on.
    about_host = _naming(lines, "nowhere.invalid")
    connecting = [line for line in about_host if "https://nowhere.invalid" in line]
    failures = [line for line in about_host if line not in connecting]
    assert len(connecting) == len(calls)
    assert len(failures) == len(calls)


def test_turn_that_recovers_by_retry_prints_nothing_at_level_1(monkeypatch, sink, tmp_path):
    lines, level = sink
    level["value"] = 1
    calls = _flaky_model(monkeypatch, [_dns_failure()])

    _turn(tmp_path, ToolContext(cwd=tmp_path))

    assert len(calls) == 2
    assert _naming(lines, "nowhere.invalid") == []


def test_turn_counts_response_bytes_into_its_turn_status(sink, tmp_path):
    context = ToolContext(cwd=tmp_path)
    context.turn_status = _RecordingStatus()

    async def drive():
        server = await asyncio.start_server(_serve_sse, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        cfg = dataclasses.replace(_cfg(tmp_path, model="t"),
                                  provider_base_url=f"http://127.0.0.1:{port}/v1")
        try:
            await runtime.run_turn_async(
                cfg, "sys", [{"role": "user", "content": "go"}],
                runtime.Telemetry(debug_log=None), tool_context=context, suppress_output=True,
            )
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(asyncio.wait_for(drive(), 20))

    history = context.turn_status.__dict__["history"]
    assert len([n for n in history if n > 0]) >= 2
    assert context.turn_status.net_bytes == 0          # zeroed at turn end
