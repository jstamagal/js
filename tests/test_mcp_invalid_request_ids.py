from __future__ import annotations

import asyncio

import pytest

from js.mcp.protocol import INVALID_REQUEST, JSONRPCPeer, REQUEST_CANCELLED
from test_mcp_protocol import FakeTransport

INVALID_IDS = [None, 1.5, 1.0, True, False, [], {}, [1], {"value": 1}]


@pytest.mark.parametrize("request_id", INVALID_IDS)
@pytest.mark.parametrize("params", [{}, []])
def test_invalid_request_id_gets_null_error_and_peer_handles_next_request(request_id, params):
    async def scenario():
        transport = FakeTransport()
        peer = await JSONRPCPeer.connect(lambda: transport)
        try:
            await transport.server_send({"jsonrpc": "2.0", "id": request_id, "method": "ping", "params": params})
            response = await transport.next_sent()
            assert response["id"] is None
            assert response["error"]["code"] == INVALID_REQUEST
            await transport.server_send({"jsonrpc": "2.0", "id": "valid", "method": "ping"})
            assert await transport.next_sent() == {"jsonrpc": "2.0", "id": "valid", "result": {}}
            assert not peer.closed
        finally:
            await peer.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("response_id", INVALID_IDS)
def test_invalid_response_id_cannot_close_peer_or_match_pending_request(response_id):
    async def scenario():
        transport = FakeTransport()
        peer = await JSONRPCPeer.connect(lambda: transport)
        request = asyncio.create_task(peer.request("ping", timeout=1))
        try:
            sent = await transport.next_sent()
            await transport.server_send({"jsonrpc": "2.0", "id": response_id, "result": "malformed"})
            await transport.server_send({"jsonrpc": "2.0", "id": sent["id"], "result": "valid"})
            assert await request == "valid"
            assert not peer.closed
        finally:
            await peer.close()
            await asyncio.gather(request, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel_id", INVALID_IDS)
def test_invalid_cancel_id_cannot_cancel_valid_integer_request(cancel_id):
    async def scenario():
        transport = FakeTransport()
        peer = await JSONRPCPeer.connect(lambda: transport)
        started = asyncio.Event()
        release = asyncio.Event()

        async def hold(_params):
            started.set()
            await release.wait()
            return "finished"

        peer.add_request_handler("hold", hold)
        try:
            await transport.server_send({"jsonrpc": "2.0", "id": 1, "method": "hold"})
            await asyncio.wait_for(started.wait(), 1)
            original = peer._incoming[1]
            await transport.server_send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": cancel_id}})
            await transport.server_send({"jsonrpc": "2.0", "id": "barrier", "method": "ping"})
            assert await transport.next_sent() == {"jsonrpc": "2.0", "id": "barrier", "result": {}}
            assert peer._incoming[1] is original and not original.done()
            await transport.server_send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}})
            result = await transport.next_sent()
            assert result["id"] == 1
            assert result["error"]["code"] == REQUEST_CANCELLED
            assert not peer.closed
        finally:
            release.set()
            await peer.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("request_id", [0, "", "text", -1])
def test_valid_ids_and_notifications_keep_existing_semantics(request_id):
    async def scenario():
        transport = FakeTransport()
        peer = await JSONRPCPeer.connect(lambda: transport)
        seen = asyncio.Event()
        peer.add_notification_handler("note", lambda _params: seen.set())
        try:
            await transport.server_send({"jsonrpc": "2.0", "method": "note"})
            await asyncio.wait_for(seen.wait(), 1)
            assert transport.messages == []
            await transport.server_send({"jsonrpc": "2.0", "method": "ping", "id": request_id})
            assert await transport.next_sent() == {"jsonrpc": "2.0", "id": request_id, "result": {}}
        finally:
            await peer.close()

    asyncio.run(scenario())
