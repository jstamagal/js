from __future__ import annotations

import asyncio

import pytest

from js.mcp.client import MCPClient, NotInitializedError, ProtocolVersionError
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
        peer.on_notification("notifications/message", lambda params: seen.append(params))
        await transport.server_send(
            {"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "info"}}
        )
        await asyncio.sleep(0)
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
