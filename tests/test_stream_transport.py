"""Streaming HTTP teardown through the actual provider SDKs."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import gc
import json
import logging

import ai
import pytest

from js import model_client, stream_transport


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.parametrize("cancel", [False, True])
def test_sdk_closes_unfinished_response_body(provider, cancel, caplog):
    async def drive():
        connections = set()
        handlers = set()
        loop_errors = []
        loop = asyncio.get_running_loop()
        previous = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))

        async def serve(reader, writer):
            connections.add(writer)
            handlers.add(asyncio.current_task())
            try:
                head = await reader.readuntil(b"\r\n\r\n")
                content_length = next(
                    int(line.split(b":", 1)[1])
                    for line in head.split(b"\r\n")
                    if line.lower().startswith(b"content-length:")
                )
                await reader.readexactly(content_length)
                if provider == "openai":
                    chunks = [
                        {
                            "id": "audit",
                            "object": "chat.completion.chunk",
                            "created": 1,
                            "model": "audit",
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"role": "assistant", "content": "OK"},
                                    "finish_reason": None,
                                }
                            ],
                        },
                        {
                            "id": "audit",
                            "object": "chat.completion.chunk",
                            "created": 1,
                            "model": "audit",
                            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                        },
                        {
                            "id": "audit",
                            "object": "chat.completion.chunk",
                            "created": 1,
                            "model": "audit",
                            "choices": [],
                            "usage": {
                                "prompt_tokens": 10,
                                "completion_tokens": 1,
                                "total_tokens": 11,
                            },
                        },
                    ]
                    body = b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks)
                    body += b"data: [DONE]\n\n"
                else:
                    events = [
                        {
                            "type": "message_start",
                            "message": {
                                "id": "audit",
                                "type": "message",
                                "role": "assistant",
                                "model": "audit",
                                "content": [],
                                "stop_reason": None,
                                "stop_sequence": None,
                                "usage": {"input_tokens": 10, "output_tokens": 0},
                            },
                        },
                        {
                            "type": "content_block_start",
                            "index": 0,
                            "content_block": {"type": "text", "text": ""},
                        },
                        {
                            "type": "content_block_delta",
                            "index": 0,
                            "delta": {"type": "text_delta", "text": "OK"},
                        },
                        {"type": "content_block_stop", "index": 0},
                        {
                            "type": "message_delta",
                            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                            "usage": {"output_tokens": 1},
                        },
                        {"type": "message_stop"},
                    ]
                    body = b"".join(
                        b"event: "
                        + e["type"].encode()
                        + b"\ndata: "
                        + json.dumps(e).encode()
                        + b"\n\n"
                        for e in events
                    )
                # Terminator arrives but HTTP body stays unfinished until client closes.
                length = len(body) if provider == "anthropic" and not cancel else 999999
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: "
                    + str(length).encode() + b"\r\n\r\n" + body
                )
                await writer.drain()
                await reader.read()
            except (ConnectionError, asyncio.IncompleteReadError):
                pass
            finally:
                writer.close()
                with suppress(ConnectionError):
                    await writer.wait_closed()
                connections.discard(writer)
                handlers.discard(asyncio.current_task())

        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        try:
            for _ in range(6):

                def text(_chunk):
                    if cancel:
                        raise asyncio.CancelledError()

                call = model_client.stream_model_async(
                    model_id="audit",
                    provider_id=provider,
                    provider_base_url=f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1",
                    provider_api_key="fixture",
                    messages=[ai.user_message("hello")],
                    tools=None,
                    max_output_tokens=32,
                    reasoning_effort=None,
                    on_text=text,
                )
                if cancel:
                    with pytest.raises(asyncio.CancelledError):
                        await asyncio.wait_for(call, 5)
                else:
                    result = await asyncio.wait_for(call, 5)
                    assert result.text == "OK"
                # Inspect before GC: actual lower byte iterators must already be closed.
                lower = [
                    g
                    for g in loop._asyncgens
                    if g.ag_frame and "httpcore2/" in g.ag_code.co_filename
                ]
                assert lower == []
            gc.collect()
            await asyncio.sleep(0)
            await loop.shutdown_asyncgens()
            if handlers:
                await asyncio.wait_for(asyncio.gather(*list(handlers)), 3)
            assert connections == set()
            assert loop_errors == []
        finally:
            server.close()
            await server.wait_closed()
            for writer in connections:
                writer.close()
            if handlers:
                await asyncio.gather(*list(handlers), return_exceptions=True)
            loop.set_exception_handler(previous)

    with caplog.at_level(logging.ERROR, logger="asyncio"):
        asyncio.run(drive())
    assert caplog.records == []


def test_owned_iterator_closes_transport_even_if_iterator_teardown_fails():
    class Body:
        closed = 0

        async def __aiter__(self):
            try:
                yield b"first"
                yield b"second"
            finally:
                raise ValueError("cleanup failure")

        async def aclose(self):
            self.closed += 1

    async def drive():
        body = Body()
        owned = stream_transport._OwnedCoreStream(body)
        assert await anext(owned) == b"first"
        with pytest.raises(ValueError, match="cleanup failure"):
            await owned.aclose()
        assert body.closed == 1
        await owned.aclose()
        assert body.closed == 1

    asyncio.run(drive())
