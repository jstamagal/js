from __future__ import annotations

import asyncio
import sys
import textwrap

import pytest

from js.mcp.client import MCPClient, MCPClientError, TransportError
from js.mcp.protocol import PeerClosedError
from js.mcp.transports import StdioTransport


SERVER = textwrap.dedent("""
    import json
    import sys

    pending = []
    counts = {'tools/call': 0, 'prompts/list': 0}
    def reply(request_id, result):
        print(json.dumps({'jsonrpc': '2.0', 'id': request_id, 'result': result}), flush=True)

    for line in sys.stdin:
        message = json.loads(line)
        if 'id' not in message:
            continue
        method = message['method']
        request_id = message['id']
        if method == 'initialize':
            reply(request_id, {'protocolVersion': '2025-06-18',
                              'capabilities': {'tools': {}, 'prompts': {}},
                              'serverInfo': {'name': 'timeout-test', 'version': '1'}})
        elif method == 'prompts/list' or (method == 'tools/call' and message['params']['name'] == 'slow'):
            counts[method] += 1
            pending.append(request_id)
        elif method == 'tools/call':
            # Deliberately deliver late replies before the fresh request's reply.
            for old_id in pending:
                reply(old_id, {'late': True})
            pending.clear()
            reply(request_id, {'content': [], 'structuredContent': counts})
        elif method == 'tools/list':
            reply(request_id, {'tools': []})
""")


@pytest.mark.parametrize("method", ["tools/call", "prompts/list"])
def test_request_timeout_preserves_server_state_and_does_not_retry(method):
    async def scenario():
        transports = []

        def factory():
            transport = StdioTransport(sys.executable, ("-c", SERVER))
            transports.append(transport)
            return transport

        client = MCPClient(factory, reconnect_backoff=0, reconnect_attempts=2)
        try:
            await client.initialize(timeout=2)
            original_peer = client.peer
            process = transports[0].process
            assert process is not None
            if method == "tools/call":
                call = client.call_tool("slow", {}, timeout=0.05)
            else:
                call = client.list_prompts(timeout=0.05)
            with pytest.raises(MCPClientError, match="TimeoutError"):
                await call
            assert client.peer is original_peer
            assert client.initialized
            assert process.returncode is None
            assert original_peer._pending == {}
            result = await client.call_tool("stats", {}, timeout=2)
            assert result.structured_content[method] == 1
            assert await client.list_tools(timeout=2) == []
            assert client.peer is original_peer
            assert transports[0].process is process
            assert len(transports) == 1
            assert original_peer._pending == {}
        finally:
            await client.close()
        assert all(transport.process.returncode == 0 for transport in transports)

    asyncio.run(scenario())


@pytest.mark.parametrize("error", [TransportError("timeout"), PeerClosedError(), ConnectionError(), OSError()])
def test_real_transport_failures_still_reconnect(error):
    assert MCPClient._is_transport_failure(error)
