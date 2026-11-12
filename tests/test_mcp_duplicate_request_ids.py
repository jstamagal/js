from __future__ import annotations

import asyncio

import pytest

from js.mcp.protocol import INVALID_REQUEST, JSONRPCPeer, REQUEST_CANCELLED
from test_mcp_protocol import FakeTransport


@pytest.mark.parametrize("request_id", [1, "same-id"])
@pytest.mark.parametrize("duplicate_method", ["hold", "ping"])
def test_duplicate_active_request_id_preserves_original_task_and_cancellation(request_id, duplicate_method):
    async def scenario():
        transport = FakeTransport()
        peer = await JSONRPCPeer.connect(lambda: transport)
        started = asyncio.Event()
        release = asyncio.Event()
        workers = []

        async def hold(_params):
            workers.append(asyncio.current_task())
            started.set()
            await release.wait()
            return "finished"

        peer.add_request_handler("hold", hold)
        try:
            await transport.server_send({"jsonrpc": "2.0", "id": request_id, "method": "hold"})
            await asyncio.wait_for(started.wait(), 1)
            original = peer._incoming[request_id]
            await transport.server_send({"jsonrpc": "2.0", "id": request_id, "method": duplicate_method})
            response = await transport.next_sent()
            assert response["id"] == request_id
            assert response["error"]["code"] == INVALID_REQUEST
            assert peer._incoming[request_id] is original
            assert len(workers) == 1
            await transport.server_send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": request_id}})
            cancelled = await transport.next_sent()
            assert cancelled["id"] == request_id
            assert cancelled["error"]["code"] == REQUEST_CANCELLED
            await transport.server_send({"jsonrpc": "2.0", "id": "next", "method": "ping"})
            assert await transport.next_sent() == {"jsonrpc": "2.0", "id": "next", "result": {}}
            assert not peer.closed
            assert original.done()
            assert request_id not in peer._incoming
        finally:
            release.set()
            await peer.close()
            await asyncio.gather(*workers, return_exceptions=True)

    asyncio.run(scenario())
