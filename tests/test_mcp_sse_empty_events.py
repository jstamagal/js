from __future__ import annotations

import io

import pytest

from js.mcp.transports import StreamableHTTPTransport, StreamableHTTPTransportError


@pytest.mark.parametrize("frame", [
    b"data:\n\n", b"id: prime\ndata:\n\n", b"id: prime\nretry: 250\ndata: \n\n",
])
def test_empty_sse_data_is_ignored_but_resume_fields_are_retained(frame):
    transport = StreamableHTTPTransport("https://example.test/mcp")
    received = []
    transport._consume_sse(io.BytesIO(frame), received.append)
    assert received == []
    if b"id:" in frame:
        assert transport.last_event_id == "prime"
        assert transport._get_last_event_id == "prime"
    if b"retry:" in frame:
        assert transport._retry_seconds == 0.25


def test_empty_priming_then_multiline_message_is_delivered():
    transport = StreamableHTTPTransport("https://example.test/mcp")
    received = []
    stream = (b"id: prime\nretry: 250\ndata:\n\n"
              b"id: next\ndata: {\"jsonrpc\": \"2.0\",\ndata: \"method\": \"ping\"}\n\n")
    transport._consume_sse(io.BytesIO(stream), received.append)
    assert received == [{"jsonrpc": "2.0", "method": "ping"}]
    assert transport.last_event_id == "next"
    assert transport._get_last_event_id == "next"
    assert transport._retry_seconds == 0.25


@pytest.mark.parametrize("frame", [b"data: broken\n\n", b"data:  \n\n", b"data:\ndata:\n\n"])
def test_nonempty_invalid_sse_data_remains_protocol_error(frame):
    transport = StreamableHTTPTransport("https://example.test/mcp")
    with pytest.raises(StreamableHTTPTransportError):
        transport._consume_sse(io.BytesIO(frame), lambda _message: pytest.fail("invalid event emitted"))


def test_empty_post_event_retains_id_without_changing_get_resume():
    transport = StreamableHTTPTransport("https://example.test/mcp")
    before_retry = transport._retry_seconds
    transport._consume_sse(io.BytesIO(b"id: post\nretry: 250\ndata:\n\n"),
                           lambda _message: pytest.fail("empty event emitted"), resume_get=False)
    assert transport.last_event_id == "post"
    assert transport._get_last_event_id is None
    assert transport._retry_seconds == before_retry
