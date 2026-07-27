from __future__ import annotations

import asyncio
import json
import logging
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from js.mcp.transports import StreamableHTTPTransport, StreamableHTTPTransportError
from js.mcp.types import LATEST_PROTOCOL_VERSION

SECRET = "HTTP_AUTH_SENTINEL"
HEADER_SECRET = "HTTP_HEADER_SENTINEL"
SESSION = "session-one"


def run(coro):
    return asyncio.run(coro)


class State:
    def __init__(self):
        self.requests: list[tuple[str, dict[str, str], object]] = []
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.notification: dict | None = None
        self.reconnect = False
        self.timeout_reconnect = False
        self.gets = 0
        self.deleted = threading.Event()
        self.delay_post_headers = False
        self.post_started = threading.Event()
        self.release_post = threading.Event()
        self.post_finished = threading.Event()

    def record(self, method, headers, body=None):
        with self.lock:
            self.requests.append((method, dict(headers), body))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def state(self) -> State:
        return self.server.state

    def log_message(self, _format, *_args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        self.state.record("POST", self.headers, body)
        method = body.get("method")
        if self.state.delay_post_headers:
            self.state.post_started.set()
            self.state.release_post.wait(3)
        if method == "test/http-error":
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if method == "test/protocol-error":
            self._body(200, "application/json", b'{"not":"jsonrpc"}')
            return
        if method == "test/request-202":
            self._body(202, "application/json", b"")
            return
        if method == "test/notification-200" or body.get("id") == "response-200":
            self._body(200, "application/json", b'{"jsonrpc":"2.0","id":null,"result":{}}')
            return
        reply = {"jsonrpc": "2.0", "id": body.get("id"), "result": {"ok": method}}
        encoded = json.dumps(reply).encode()
        if method in {"test/sse", "test/sse-long", "test/sse-invalid"}:
            payload = b"id: post-one\nretry: 5\ndata: " + encoded + b"\n\n"
            if method == "test/sse-invalid":
                payload = b"data: {not-json}\n\n"
            if method == "test/sse-long":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Mcp-Session-Id", SESSION)
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(payload)
                self.wfile.flush()
                self.state.stop.wait(3)
            else:
                self._body(200, "text/event-stream; charset=utf-8", payload, session=True)
        elif "id" in body:
            self._body(200, "application/json", encoded, session=True)
        else:
            self._body(202, "application/json", b"", session=True)
        if self.state.delay_post_headers:
            self.state.post_finished.set()

    def do_GET(self):
        self.state.gets += 1
        self.state.record("GET", self.headers)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        if self.state.reconnect and self.state.gets == 1:
            self.wfile.write(b"id: event-one\nretry: 1\ndata: {\"jsonrpc\":\"2.0\",\"method\":\"first\"}\n\n")
            self.wfile.flush()
            return
        if self.state.timeout_reconnect and self.state.gets == 1:
            self.state.stop.wait(0.2)
            return
        notification = self.state.notification
        if notification is not None:
            payload = json.dumps(notification).encode()
            event_id = b"event-two" if self.state.reconnect else b"notification-one"
            self.wfile.write(b"id: " + event_id + b"\ndata: " + payload + b"\n\n")
            self.wfile.flush()
            self.state.notification = None
        self.state.stop.wait(3)

    def do_DELETE(self):
        self.state.record("DELETE", self.headers)
        self.state.deleted.set()
        self.state.stop.set()
        self._body(204, "application/json", b"")

    def _body(self, status, content_type, body, *, session=False):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if session:
            self.send_header("Mcp-Session-Id", SESSION)
        self.end_headers()
        if body:
            self.wfile.write(body)


@contextmanager
def server():
    state = State()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.state = state
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, f"http://127.0.0.1:{httpd.server_port}/mcp"
    finally:
        state.stop.set()
        httpd.shutdown()
        httpd.server_close()
        thread.join(2)


def request(method="test/json", request_id=1):
    return {"jsonrpc": "2.0", "id": request_id, "method": method}


def test_json_and_sse_post_replies_track_session_and_event_fields():
    async def scenario(url):
        transport = StreamableHTTPTransport(url)
        await transport.send(request())
        assert await transport.receive() == {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"ok": "test/json"},
        }
        assert transport.session_id == SESSION
        await transport.send(request("test/sse", 2))
        assert (await transport.receive())["id"] == 2
        assert transport.last_event_id == "post-one"
        await transport.close()

    with server() as (state, url):
        run(scenario(url))
        posts = [entry for entry in state.requests if entry[0] == "POST"]
        first_headers = posts[0][1]
        second_headers = posts[1][1]
        assert first_headers["Accept"] == "application/json, text/event-stream"
        assert first_headers["Content-Type"] == "application/json"
        assert first_headers["Mcp-Protocol-Version"] == LATEST_PROTOCOL_VERSION
        assert "Mcp-Session-Id" not in first_headers
        assert second_headers["Mcp-Session-Id"] == SESSION


def test_long_lived_sse_post_returns_without_waiting_for_stream_eof():
    async def scenario(url):
        transport = StreamableHTTPTransport(url)
        await asyncio.wait_for(transport.send(request("test/sse-long")), 0.5)
        assert (await asyncio.wait_for(transport.receive(), 0.5))["id"] == 1
        await asyncio.wait_for(transport.close(), 1)

    with server() as (_state, url):
        run(scenario(url))


def test_get_delivers_notification_and_delete_shuts_down_issued_session():
    async def scenario(url):
        transport = StreamableHTTPTransport(url)
        await transport.send(request())
        await transport.receive()
        notification = await asyncio.wait_for(transport.receive(), 1)
        assert notification["method"] == "notifications/tools/list_changed"
        await transport.close()

    with server() as (state, url):
        state.notification = {
            "jsonrpc": "2.0",
            "method": "notifications/tools/list_changed",
            "params": {},
        }
        run(scenario(url))
        assert state.deleted.wait(1)
        gets = [entry for entry in state.requests if entry[0] == "GET"]
        deletes = [entry for entry in state.requests if entry[0] == "DELETE"]
        assert gets[0][1]["Mcp-Session-Id"] == SESSION
        assert deletes[0][1]["Mcp-Session-Id"] == SESSION


def test_notification_stream_reconnects_with_last_event_id():
    async def scenario(url):
        transport = StreamableHTTPTransport(url, reconnect_backoff=0, reconnect_attempts=2)
        await transport.send(request())
        await transport.receive()
        assert (await asyncio.wait_for(transport.receive(), 1))["method"] == "first"
        assert (await asyncio.wait_for(transport.receive(), 1))["method"] == "after-reconnect"
        await transport.close()

    with server() as (state, url):
        state.reconnect = True
        state.notification = {"jsonrpc": "2.0", "method": "after-reconnect"}
        run(scenario(url))
        gets = [entry for entry in state.requests if entry[0] == "GET"]
        assert len(gets) >= 2
        assert gets[1][1]["Last-Event-Id"] == "event-one"


def test_idle_notification_stream_timeout_reconnects():
    async def scenario(url):
        transport = StreamableHTTPTransport(
            url, timeout=0.05, reconnect_backoff=0, reconnect_attempts=2
        )
        await transport.send(request())
        await transport.receive()
        assert (await asyncio.wait_for(transport.receive(), 1))["method"] == "after-timeout"
        await transport.close()

    with server() as (state, url):
        state.timeout_reconnect = True
        state.notification = {"jsonrpc": "2.0", "method": "after-timeout"}
        run(scenario(url))
        assert state.gets >= 2


@pytest.mark.parametrize(
    "message",
    [
        request("test/request-202"),
        {"jsonrpc": "2.0", "method": "test/notification-200"},
        {"jsonrpc": "2.0", "id": "response-200", "result": {}},
    ],
)
def test_post_rejects_status_semantics_mismatches(message):
    async def scenario(url):
        transport = StreamableHTTPTransport(url)
        with pytest.raises(StreamableHTTPTransportError, match="invalid response"):
            await transport.send(message)
        await transport.close()

    with server() as (_state, url):
        run(scenario(url))


def test_invalid_sse_post_event_reports_safe_protocol_failure():
    async def scenario(url):
        transport = StreamableHTTPTransport(
            url,
            headers={"Authorization": f"Bearer {SECRET}", "X-Private": HEADER_SECRET},
            name=f"configured {SECRET} {HEADER_SECRET}",
        )
        await transport.send(request("test/sse-invalid"))
        with pytest.raises(StreamableHTTPTransportError, match="invalid protocol JSON") as caught:
            await asyncio.wait_for(transport.receive(), 1)
        assert SECRET not in str(caught.value)
        assert HEADER_SECRET not in str(caught.value)
        await transport.close()

    with server() as (_state, url):
        run(scenario(url))


@pytest.mark.parametrize(
    ("method", "match"),
    [("test/http-error", "HTTP 503"), ("test/protocol-error", "invalid protocol JSON")],
)
def test_http_and_protocol_failures_are_safe(method, match, caplog):
    async def scenario(url):
        transport = StreamableHTTPTransport(
            url,
            headers={"Authorization": f"Bearer {SECRET}", "X-Private": HEADER_SECRET},
            name=f"configured {SECRET} {HEADER_SECRET}",
        )
        with pytest.raises(StreamableHTTPTransportError, match=match) as caught:
            await transport.send(request(method))
        assert SECRET not in str(caught.value)
        assert HEADER_SECRET not in str(caught.value)
        await transport.close()

    with server() as (_state, url), caplog.at_level(logging.DEBUG):
        run(scenario(url))
    assert SECRET not in caplog.text
    assert HEADER_SECRET not in caplog.text


def test_cancelled_post_before_headers_remains_owned_through_close():
    async def scenario(state, url):
        transport = StreamableHTTPTransport(url, timeout=1)
        sending = asyncio.create_task(transport.send(request()))
        assert await asyncio.to_thread(state.post_started.wait, 1)
        sending.cancel()
        closing = asyncio.create_task(transport.close())
        with pytest.raises(asyncio.CancelledError):
            await sending
        # close() no longer waits out the header-blocked POST: it shuts the
        # tracked socket down, the request thread errors out promptly, and
        # ownership is still settled before close returns.
        await asyncio.wait_for(closing, 2)
        assert not transport._request_tasks
        assert not transport._active
        state.release_post.set()

    with server() as (state, url):
        state.delay_post_headers = True
        run(scenario(state, url))


def test_cancellation_and_close_unblock_long_lived_get():
    async def scenario(url):
        transport = StreamableHTTPTransport(url)
        await transport.send(request())
        await transport.receive()
        receive = asyncio.create_task(transport.receive())
        await asyncio.sleep(0.05)
        receive.cancel()
        with pytest.raises(asyncio.CancelledError):
            await receive
        await asyncio.wait_for(transport.close(), 1)

    with server() as (_state, url):
        run(scenario(url))


def test_close_unblocks_send_still_waiting_for_response_headers():
    async def drive():
        import contextlib
        import socket as socketlib
        import time

        listener = socketlib.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        accepted = []

        def serve():
            with contextlib.suppress(Exception):
                conn, _ = listener.accept()
                accepted.append(conn)
                conn.recv(65536)  # swallow the request, never send headers

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        transport = StreamableHTTPTransport(
            f"http://127.0.0.1:{port}/mcp", name="hung", timeout=30.0
        )
        await transport.start()
        send_task = asyncio.create_task(transport.send(request()))
        deadline = time.monotonic() + 5
        while not accepted and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert accepted, "server never accepted the connection"
        await asyncio.sleep(0.05)
        started = time.monotonic()
        await asyncio.wait_for(transport.close(), 5)
        assert time.monotonic() - started < 3, "close blocked on the header wait"
        with pytest.raises((StreamableHTTPTransportError, asyncio.CancelledError)):
            await asyncio.wait_for(send_task, 5)
        for conn in accepted:
            with contextlib.suppress(Exception):
                conn.close()
        listener.close()

    run(drive())


def test_close_unblocks_https_operation_stalled_in_tls_handshake():
    async def drive():
        import contextlib
        import socket as socketlib
        import time

        listener = socketlib.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        accepted = []

        def serve():
            with contextlib.suppress(Exception):
                conn, _ = listener.accept()
                accepted.append(conn)
                conn.recv(65536)  # swallow ClientHello, never answer

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        transport = StreamableHTTPTransport(
            f"https://127.0.0.1:{port}/mcp", name="tls-hang", timeout=30.0
        )
        await transport.start()
        send_task = asyncio.create_task(transport.send(request()))
        deadline = time.monotonic() + 5
        while not accepted and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert accepted, "server never accepted the TLS connection"
        await asyncio.sleep(0.05)
        started = time.monotonic()
        await asyncio.wait_for(transport.close(), 5)
        assert time.monotonic() - started < 3, "close blocked on the TLS handshake"
        with pytest.raises((StreamableHTTPTransportError, asyncio.CancelledError)):
            await asyncio.wait_for(send_task, 5)
        for conn in accepted:
            with contextlib.suppress(Exception):
                conn.close()
        listener.close()

    run(drive())


def test_sse_id_commits_only_at_event_boundary_and_eof_discards_partial():
    class FakeStream:
        def __init__(self, payload: bytes):
            self._lines = payload.splitlines(keepends=True)

        def read1(self, _limit):
            return self._lines.pop(0) if self._lines else b""

    transport = StreamableHTTPTransport("http://127.0.0.1:1/mcp")
    received = []

    complete = b'id: one\ndata: {"jsonrpc":"2.0","method":"a"}\n\n'
    partial = b'id: lost\ndata: {"jsonrpc":"2.0","method":"b"}\n'  # no boundary
    transport._consume_sse(FakeStream(complete + partial), received.append)

    assert [m["method"] for m in received] == ["a"]
    assert transport.last_event_id == "one"


class ReadlineStream:
    """read1() delivering LF-split chunks, like a buffered HTTP response."""

    def __init__(self, payload: bytes):
        self._payload = payload
        self._pos = 0

    def read1(self, _limit):
        if self._pos >= len(self._payload):
            return b""
        index = self._payload.find(b"\n", self._pos)
        end = len(self._payload) if index == -1 else index + 1
        chunk = self._payload[self._pos:end]
        self._pos = end
        return chunk


def test_sse_grammar_cr_terminators_bom_and_empty_id_reset():
    received = []
    transport = StreamableHTTPTransport("http://127.0.0.1:1/mcp")

    # Lone-CR terminated stream with a leading BOM.
    cr_stream = (
        b'\xef\xbb\xbfid: cr-one\rdata: {"jsonrpc":"2.0","method":"a"}\r\r'
    )
    transport._consume_sse(ReadlineStream(cr_stream), received.append)
    assert [m["method"] for m in received] == ["a"]
    assert transport.last_event_id == "cr-one"

    # A completed empty id resets resume state to the empty string.
    reset_stream = b"id\ndata: {\"jsonrpc\":\"2.0\",\"method\":\"b\"}\n\n"
    transport._consume_sse(ReadlineStream(reset_stream), received.append)
    assert [m["method"] for m in received] == ["a", "b"]
    assert transport.last_event_id == ""

    # Non-digit retry values are ignored per the grammar.
    before = transport._retry_seconds
    transport._consume_sse(ReadlineStream(b"retry: -5\nretry: x\n\n"), received.append)
    assert transport._retry_seconds == before


def test_lone_cr_events_deliver_promptly_on_a_live_stream():
    class Read1Stream:
        """Each read1 call is one network arrival; EOF only at the end."""

        def __init__(self, chunks, probe):
            self._chunks = list(chunks)
            self._probe = probe

        def read1(self, _limit):
            self._probe()
            return self._chunks.pop(0) if self._chunks else b""

    received = []
    deliveries_before_read = []
    transport = StreamableHTTPTransport("http://127.0.0.1:1/mcp")
    chunks = [
        b'id: live\rdata: {"jsonrpc":"2.0","method":"a"}\r\r',
        b'data: {"jsonrpc":"2.0","method":"b"}\r\r',
    ]
    stream = Read1Stream(chunks, lambda: deliveries_before_read.append(len(received)))
    transport._consume_sse(stream, received.append)
    assert [m["method"] for m in received] == ["a", "b"]
    # The first event was delivered before the second network read, and the
    # second before the EOF read: no waiting for LF or EOF.
    assert deliveries_before_read == [0, 1, 2]
    assert transport.last_event_id == "live"


def test_crlf_and_bom_split_across_network_reads():
    class Read1Stream:
        def __init__(self, chunks):
            self._chunks = list(chunks)

        def read1(self, _limit):
            return self._chunks.pop(0) if self._chunks else b""

    received = []
    transport = StreamableHTTPTransport("http://127.0.0.1:1/mcp")
    chunks = [
        b"\xef",
        b"\xbb\xbfdata: {\"jsonrpc\":\"2.0\",\"method\":\"x\"}\r",
        b"\n\r\n",
    ]
    transport._consume_sse(Read1Stream(chunks), received.append)
    assert [m["method"] for m in received] == ["x"]
