from __future__ import annotations

import asyncio
import threading

import pytest
from test_mcp_http_transport import finish_get_stream, open_get_stream, request, scripted

from js.mcp.transports import StreamableHTTPTransport, StreamableHTTPTransportError


@pytest.mark.parametrize("frame", [
    b"data:\n\n", b"id: prime\ndata:\n\n", b"id: prime\nretry: 250\ndata: \n\n",
])
def test_empty_sse_data_is_ignored_but_resume_fields_are_retained(frame):
    async def scenario(state, url):
        transport = await open_get_stream(url)
        await finish_get_stream(transport, state)
        if b"id:" in frame:
            assert transport.last_event_id == "prime"

    with scripted(frame) as (state, url):
        asyncio.run(scenario(state, url))
        if b"id:" in frame:
            assert state.get_resume[1] == "prime"
        if b"retry:" in frame:
            assert state.get_times[1] - state.get_times[0] >= 0.25


def test_empty_priming_then_multiline_message_is_delivered():
    stream = (b"id: prime\nretry: 250\ndata:\n\n"
              b"id: next\ndata: {\"jsonrpc\": \"2.0\",\ndata: \"method\": \"ping\"}\n\n")

    async def scenario(state, url):
        transport = await open_get_stream(url)
        assert await asyncio.wait_for(transport.receive(), 1) == {"jsonrpc": "2.0", "method": "ping"}
        await finish_get_stream(transport, state)
        assert transport.last_event_id == "next"

    with scripted(stream) as (state, url):
        asyncio.run(scenario(state, url))
        assert state.get_resume[1] == "next"
        assert state.get_times[1] - state.get_times[0] >= 0.25


@pytest.mark.parametrize("frame", [b"data: broken\n\n", b"data:  \n\n", b"data:\ndata:\n\n"])
def test_nonempty_invalid_sse_data_remains_protocol_error(frame):
    async def scenario(url):
        transport = await open_get_stream(url)
        # The failure is the next thing received: no event was emitted first.
        with pytest.raises(StreamableHTTPTransportError):
            await asyncio.wait_for(transport.receive(), 1)
        await transport.close()

    with scripted(frame) as (_state, url):
        asyncio.run(scenario(url))


def test_empty_post_event_retains_id_without_changing_get_resume():
    get_may_end = threading.Event()

    async def scenario(state, url):
        transport = StreamableHTTPTransport(url, reconnect_backoff=0)
        await transport.send(request("test/sse-script"))
        # The reply follows the empty event on the same stream.
        assert (await asyncio.wait_for(transport.receive(), 1))["result"] == {"ok": "script"}
        assert transport.last_event_id == "post"
        get_may_end.set()
        # A POST stream's retry of 30s would hold the reconnect past this wait.
        await finish_get_stream(transport, state)

    with scripted([get_may_end], post_frame=b"id: post\nretry: 30000\ndata:\n\n") as (state, url):
        asyncio.run(scenario(state, url))
        assert state.get_resume == [None, None]
