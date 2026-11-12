from __future__ import annotations

import asyncio

import pytest

from js.mcp.client import CapabilityError, MCPClient
from test_mcp_client import SpecServer


@pytest.mark.parametrize("resources", [True, False, None, 1, "yes", [], ["subscribe"]])
def test_invalid_resource_capability_is_rejected_during_initialization(resources):
    async def scenario():
        server = SpecServer(capabilities={"resources": resources})
        client = MCPClient(server.factory)
        try:
            with pytest.raises(ValueError):
                await client.initialize()
            assert not client.initialized
            assert all(message.get("method") not in {"resources/subscribe", "resources/unsubscribe"}
                       for _connection, message in server.messages)
        finally:
            await client.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("resources", [{}, {"subscribe": False}])
@pytest.mark.parametrize("method", ["subscribe_resource", "unsubscribe_resource"])
def test_unsupported_resource_subscription_is_explicit_error(resources, method):
    async def scenario():
        server = SpecServer(capabilities={"resources": resources})
        client = MCPClient(server.factory)
        try:
            await client.initialize()
            peer = client.peer
            with pytest.raises(CapabilityError):
                await getattr(client, method)("file:///item")
            assert client.peer is peer
            assert client.initialized
            assert not server.subscriptions
            assert all(message.get("method") not in {"resources/subscribe", "resources/unsubscribe"}
                       for _connection, message in server.messages)
            assert len(server.connections) == 1
        finally:
            await client.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("subscribe", [False, True])
def test_valid_resource_subscription_capability_preserves_behavior(subscribe):
    async def scenario():
        server = SpecServer(capabilities={"resources": {"subscribe": subscribe}})
        client = MCPClient(server.factory)
        try:
            await client.initialize()
            if subscribe:
                await client.subscribe_resource("file:///item")
                assert "file:///item" in server.subscriptions
                await client.unsubscribe_resource("file:///item")
                assert not server.subscriptions
            else:
                with pytest.raises(CapabilityError):
                    await client.subscribe_resource("file:///item")
                assert not server.subscriptions
        finally:
            await client.close()

    asyncio.run(scenario())
