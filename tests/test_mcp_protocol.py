from __future__ import annotations

import asyncio

import pytest

from js.mcp.client import MCPClient, MCPClientError, NotInitializedError, ProtocolVersionError
from js.mcp.protocol import JSONRPCError, JSONRPCPeer, PeerClosedError, REQUEST_CANCELLED
from js.mcp.types import LATEST_PROTOCOL_VERSION


class FakeTransport:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.sent = asyncio.Queue()
        self.messages = []
        self.close_calls = 0

    async def send(self, message):
        self.messages.append(message)
        await self.sent.put(message)

    async def receive(self):
        return await self.incoming.get()

    async def close(self):
        self.close_calls += 1

    async def server_send(self, message):
        await self.incoming.put(message)

    async def next_sent(self):
        return await asyncio.wait_for(self.sent.get(), 1)

    async def eof(self):
        await self.incoming.put(None)


async def make_peer():
    transport = FakeTransport()
    peer = await JSONRPCPeer.connect(lambda: transport)
    return peer, transport


def test_initialize_orders_handshake_and_advertises_only_implemented_capabilities():
    async def drive():
        transport = FakeTransport()
        client = MCPClient(lambda: transport, name="test-js", version="7")

        with pytest.raises(NotInitializedError):
            await client.request("tools/list")

        task = asyncio.create_task(client.initialize())
        initialize = await transport.next_sent()
        assert initialize == {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": LATEST_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "test-js", "version": "7"},
            },
        }
        assert transport.messages == [initialize]

        await transport.server_send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "protocolVersion": LATEST_PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": True}},
                    "serverInfo": {"name": "fake", "version": "1"},
                },
            }
        )
        result = await task
        initialized = await transport.next_sent()
        assert initialized == {"jsonrpc": "2.0", "method": "notifications/initialized"}
        assert result.capabilities.supports("tools")
        assert client.server_capabilities.raw == {"tools": {"listChanged": True}}
        await client.close()
        await client.close()
        assert transport.close_calls == 1

    asyncio.run(drive())


def test_close_racing_initialize_closes_late_transport_without_publishing_result():
    async def drive():
        transport = FakeTransport()
        release_factory = asyncio.Event()
        factory_started = asyncio.Event()

        async def delayed_factory():
            factory_started.set()
            await release_factory.wait()
            return transport

        client = MCPClient(delayed_factory)
        initialize_task = asyncio.create_task(client.initialize())
        await factory_started.wait()

        await client.close()
        release_factory.set()

        with pytest.raises(MCPClientError, match="closed"):
            await initialize_task
        assert transport.close_calls == 1
        assert client.peer is None
        assert client.initialize_result is None
        assert not client.initialized

        await client.close()
        assert transport.close_calls == 1

    asyncio.run(drive())


def test_close_during_handshake_prevents_initialize_result_publication():
    async def drive():
        class PausedInitializedTransport(FakeTransport):
            def __init__(self):
                super().__init__()
                self.initialized_started = asyncio.Event()
                self.release_initialized = asyncio.Event()

            async def send(self, message):
                if message.get("method") == "notifications/initialized":
                    self.initialized_started.set()
                    await self.release_initialized.wait()
                await super().send(message)

        transport = PausedInitializedTransport()
        client = MCPClient(lambda: transport)
        initialize_task = asyncio.create_task(client.initialize())
        request = await transport.next_sent()
        await transport.server_send(
            {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {
                    "protocolVersion": LATEST_PROTOCOL_VERSION,
                    "capabilities": {},
                    "serverInfo": {"name": "fake", "version": "1"},
                },
            }
        )
        await transport.initialized_started.wait()

        await client.close()
        transport.release_initialized.set()

        with pytest.raises(MCPClientError, match="closed"):
            await initialize_task
        assert transport.close_calls == 1
        assert client.initialize_result is None
        assert not client.initialized

    asyncio.run(drive())


def test_initialize_rejects_unsupported_version_and_invalid_capabilities():
    async def attempt(result, error):
        transport = FakeTransport()
        client = MCPClient(lambda: transport)
        task = asyncio.create_task(client.initialize())
        request = await transport.next_sent()
        await transport.server_send({"jsonrpc": "2.0", "id": request["id"], "result": result})
        with pytest.raises(error):
            await task
        assert not client.initialized
        assert not any(m.get("method") == "notifications/initialized" for m in transport.messages)
        assert transport.close_calls == 1

    async def drive():
        base = {
            "protocolVersion": LATEST_PROTOCOL_VERSION,
            "capabilities": {},
            "serverInfo": {"name": "fake", "version": "1"},
        }
        await attempt({**base, "protocolVersion": "2099-01-01"}, ProtocolVersionError)
        await attempt({**base, "capabilities": {"tools": True}}, ValueError)

    asyncio.run(drive())


def test_concurrent_requests_correlate_out_of_order_replies():
    async def drive():
        peer, transport = await make_peer()
        first = asyncio.create_task(peer.request("one", {"value": 1}))
        second = asyncio.create_task(peer.request("two", {"value": 2}))
        sent = [await transport.next_sent(), await transport.next_sent()]
        by_method = {message["method"]: message for message in sent}

        await transport.server_send(
            {"jsonrpc": "2.0", "id": by_method["two"]["id"], "result": "second"}
        )
        await transport.server_send(
            {"jsonrpc": "2.0", "id": by_method["one"]["id"], "result": "first"}
        )
        assert await asyncio.gather(first, second) == ["first", "second"]
        await peer.close()

    asyncio.run(drive())


def test_notifications_and_remote_errors_preserve_wire_data():
    async def drive():
        peer, transport = await make_peer()
        seen = []
        delivered = asyncio.Event()

        def record(params):
            seen.append(params)
            delivered.set()

        peer.on_notification("notifications/message", record)
        await transport.server_send(
            {"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "info"}}
        )
        await asyncio.wait_for(delivered.wait(), 1)
        assert seen == [{"level": "info"}]

        task = asyncio.create_task(peer.request("tools/call"))
        request = await transport.next_sent()
        await transport.server_send(
            {
                "jsonrpc": "2.0",
                "id": request["id"],
                "error": {"code": -32099, "message": "broken", "data": None},
            }
        )
        with pytest.raises(JSONRPCError) as caught:
            await task
        assert caught.value.code == -32099
        assert caught.value.message == "broken"
        assert caught.value.data is None
        assert caught.value.has_data
        assert caught.value.to_wire() == {"code": -32099, "message": "broken", "data": None}
        await peer.close()

    asyncio.run(drive())


def test_notification_handlers_can_request_without_blocking_reader_and_stay_ordered(caplog):
    async def drive():
        peer, transport = await make_peer()
        seen = []
        completed = asyncio.Event()

        async def refresh(params):
            seen.append(("start", params["sequence"]))
            request_task = asyncio.create_task(peer.request("tools/list"))
            request = await transport.next_sent()
            await transport.server_send(
                {"jsonrpc": "2.0", "id": request["id"], "result": {"tools": [params]}}
            )
            seen.append(("result", (await request_task)["tools"][0]["sequence"]))
            if params["sequence"] == 1:
                raise RuntimeError("callback broke")
            completed.set()

        peer.on_notification("notifications/tools/list_changed", refresh)
        for sequence in (1, 2):
            await transport.server_send(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/tools/list_changed",
                    "params": {"sequence": sequence},
                }
            )

        await asyncio.wait_for(completed.wait(), 1)
        assert seen == [("start", 1), ("result", 1), ("start", 2), ("result", 2)]
        assert not peer.closed
        assert "MCP notification handler failed" in caplog.text
        await peer.close()

    asyncio.run(drive())


@pytest.mark.parametrize("kind", ["notification", "request"])
def test_close_from_incoming_handler_does_not_await_itself(kind):
    async def drive():
        peer, transport = await make_peer()
        closed = asyncio.Event()

        async def close_peer(_params):
            await peer.close()
            closed.set()

        if kind == "notification":
            peer.on_notification("notifications/close", close_peer)
            message = {"jsonrpc": "2.0", "method": "notifications/close"}
        else:
            peer.on_request("close", close_peer)
            message = {"jsonrpc": "2.0", "id": "close-1", "method": "close"}
        await transport.server_send(message)

        await asyncio.wait_for(closed.wait(), 1)
        assert peer.closed
        assert transport.close_calls == 1

    asyncio.run(drive())


@pytest.mark.parametrize("cancel_kind", ["timeout", "task"])
def test_timeout_and_local_cancellation_notify_remote_peer(cancel_kind):
    async def drive():
        peer, transport = await make_peer()
        if cancel_kind == "timeout":
            task = asyncio.create_task(peer.request("slow", timeout=0.01))
            request = await transport.next_sent()
            with pytest.raises(TimeoutError):
                await task
        else:
            task = asyncio.create_task(peer.request("slow"))
            request = await transport.next_sent()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert await transport.next_sent() == {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {
                "requestId": request["id"],
                "reason": "Request cancelled by client",
            },
        }
        await peer.close()

    asyncio.run(drive())


def test_peer_originated_cancellation_stops_server_to_client_request():
    async def drive():
        peer, transport = await make_peer()
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def sample(_params):
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        peer.on_request("sampling/createMessage", sample)
        await transport.server_send(
            {
                "jsonrpc": "2.0",
                "id": "server-1",
                "method": "sampling/createMessage",
                "params": {"messages": []},
            }
        )
        await asyncio.wait_for(started.wait(), 1)
        await transport.server_send(
            {
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": "server-1", "reason": "stop"},
            }
        )
        await asyncio.wait_for(cancelled.wait(), 1)
        assert await transport.next_sent() == {
            "jsonrpc": "2.0",
            "id": "server-1",
            "error": {"code": REQUEST_CANCELLED, "message": "Request cancelled"},
        }
        await peer.close()

    asyncio.run(drive())


def test_eof_fails_pending_requests_and_shutdown_is_idempotent():
    async def drive():
        peer, transport = await make_peer()
        pending = asyncio.create_task(peer.request("never"))
        await transport.next_sent()
        await transport.eof()
        with pytest.raises(PeerClosedError, match="EOF"):
            await pending
        assert peer.closed
        await peer.close()
        await peer.close()
        assert transport.close_calls == 1

    asyncio.run(drive())


def test_failed_result_send_is_retrieved_and_logged_without_exception_text(caplog):
    async def drive():
        transport = FakeTransport()
        sentinel = "Bearer sk-super-secret-token"

        original_send = transport.send

        async def send(message):
            if message.get("id") == "srv-1" and ("result" in message or "error" in message):
                raise ConnectionError(f"send failed: {sentinel}")
            await original_send(message)

        transport.send = send
        peer = await JSONRPCPeer.connect(lambda: transport)

        async def handler(params):
            return {"ok": True}

        peer.add_request_handler("sampling/createMessage", handler)

        unretrieved = []
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda _loop, ctx: unretrieved.append(ctx))

        await transport.server_send(
            {"jsonrpc": "2.0", "id": "srv-1", "method": "sampling/createMessage", "params": {}}
        )
        for _ in range(100):
            if peer._incoming:
                break
            await asyncio.sleep(0)
        assert peer._incoming, "request task never started"
        while peer._incoming:
            await asyncio.sleep(0)
        await peer.close()
        # Force any pending unretrieved-task diagnostics to surface now.
        import gc

        gc.collect()
        await asyncio.sleep(0)
        return unretrieved, sentinel

    with caplog.at_level("WARNING", logger="js.mcp.protocol"):
        unretrieved, sentinel = asyncio.run(drive())
    assert unretrieved == []
    assert any("ConnectionError" in record.getMessage() for record in caplog.records)
    assert all(sentinel not in record.getMessage() for record in caplog.records)
