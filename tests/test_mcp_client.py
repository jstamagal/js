from __future__ import annotations

import asyncio

import pytest

from js.mcp.client import CapabilityError, MCPClient, MCPClientError
from js.mcp.types import LATEST_PROTOCOL_VERSION


class SpecServerTransport:
    def __init__(self, server, connection):
        self.server = server
        self.connection = connection
        self.incoming = asyncio.Queue()
        self.closed = False

    async def send(self, message):
        self.server.messages.append((self.connection, message))
        if "id" not in message:
            return
        if self.server.die_on == (self.connection, message["method"]):
            self.server.die_on = None
            await self.incoming.put(None)
            return
        try:
            result = self.server.handle(message["method"], message.get("params", {}))
        except Exception as exc:
            await self.incoming.put(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {"code": -32000, "message": str(exc)},
                }
            )
        else:
            await self.incoming.put(
                {"jsonrpc": "2.0", "id": message["id"], "result": result}
            )

    async def receive(self):
        return await self.incoming.get()

    async def close(self):
        self.closed = True

    async def notify(self, method, params):
        await self.incoming.put({"jsonrpc": "2.0", "method": method, "params": params})


class SpecServer:
    def __init__(self, capabilities=None):
        self.capabilities = capabilities or {
            "tools": {"listChanged": True},
            "resources": {"listChanged": True, "subscribe": True},
            "prompts": {"listChanged": True},
            "logging": {},
        }
        self.connections = []
        self.messages = []
        self.die_on = None
        self.calls = 0
        self.subscriptions = set()

    def factory(self):
        transport = SpecServerTransport(self, len(self.connections))
        self.connections.append(transport)
        return transport

    def handle(self, method, params):
        if method == "initialize":
            return {
                "protocolVersion": LATEST_PROTOCOL_VERSION,
                "capabilities": self.capabilities,
                "serverInfo": {"name": "spec-server", "version": "1"},
            }
        if method == "tools/list":
            return self.page("tools", [{"name": "alpha"}, {"name": "beta"}], params)
        if method == "resources/list":
            return self.page(
                "resources", [{"uri": "file:///a"}, {"uri": "file:///b"}], params
            )
        if method == "resources/templates/list":
            return self.page(
                "resourceTemplates",
                [{"uriTemplate": "file:///{name}"}, {"uriTemplate": "db:///{id}"}],
                params,
            )
        if method == "prompts/list":
            return self.page("prompts", [{"name": "one"}, {"name": "two"}], params)
        if method == "tools/call":
            self.calls += 1
            return {
                "content": [
                    {"type": "text", "text": "hello"},
                    {"type": "image", "data": "aW1n", "mimeType": "image/png"},
                    {"type": "audio", "data": "YXVkaW8=", "mimeType": "audio/wav"},
                    {
                        "type": "resource",
                        "resource": {"uri": "file:///a", "text": "body"},
                    },
                    {"type": "resource_link", "uri": "file:///b", "name": "b"},
                ],
                "structuredContent": {"answer": [1, 2]},
                "isError": False,
                "_meta": {"trace": "ok"},
            }
        if method == "resources/read":
            return {
                "contents": [
                    {"uri": params["uri"], "text": "text"},
                    {"uri": "file:///blob", "blob": "YmxvYg==", "mimeType": "x/test"},
                ]
            }
        if method == "resources/subscribe":
            self.subscriptions.add(params["uri"])
            return {}
        if method == "resources/unsubscribe":
            self.subscriptions.discard(params["uri"])
            return {}
        if method == "prompts/get":
            return {
                "description": "rendered",
                "messages": [
                    {"role": "user", "content": {"type": "text", "text": params["arguments"]["x"]}}
                ],
            }
        raise AssertionError(f"unexpected method {method}")

    @staticmethod
    def page(key, values, params):
        if params.get("cursor") == "page-2":
            return {key: values[1:]}
        return {key: values[:1], "nextCursor": "page-2"}


async def initialized_client(server, **kwargs):
    client = MCPClient(server.factory, reconnect_backoff=0, **kwargs)
    await client.initialize()
    return client


def requests(server, method):
    return [message for _, message in server.messages if message.get("method") == method]


def test_paginated_discovery_progress_and_structured_tool_results():
    async def drive():
        server = SpecServer()
        client = await initialized_client(server)

        assert [tool["name"] for tool in await client.list_tools(progress_token="discover")] == [
            "alpha",
            "beta",
        ]
        assert [item["uri"] for item in await client.list_resources()] == [
            "file:///a",
            "file:///b",
        ]
        assert [item["uriTemplate"] for item in await client.list_resource_templates()] == [
            "file:///{name}",
            "db:///{id}",
        ]
        assert [item["name"] for item in await client.list_prompts()] == ["one", "two"]
        assert requests(server, "tools/list")[0]["params"]["_meta"] == {
            "progressToken": "discover"
        }
        assert requests(server, "tools/list")[1]["params"]["cursor"] == "page-2"

        result = await client.call_tool("everything", {"value": 4}, progress_token=23)
        assert [block["type"] for block in result.content] == [
            "text",
            "image",
            "audio",
            "resource",
            "resource_link",
        ]
        assert result.structured_content == {"answer": [1, 2]}
        assert result.meta == {"trace": "ok"}
        call = requests(server, "tools/call")[0]
        assert call["params"] == {
            "name": "everything",
            "arguments": {"value": 4},
            "_meta": {"progressToken": 23},
        }
        await client.close()

    asyncio.run(drive())


def test_resources_subscriptions_and_prompt_arguments_preserve_wire_values():
    async def drive():
        server = SpecServer()
        client = await initialized_client(server)

        resource = await client.read_resource("file:///a")
        assert resource.contents == [
            {"uri": "file:///a", "text": "text"},
            {"uri": "file:///blob", "blob": "YmxvYg==", "mimeType": "x/test"},
        ]
        await client.subscribe_resource("file:///a")
        assert server.subscriptions == {"file:///a"}
        await client.unsubscribe_resource("file:///a")
        assert not server.subscriptions

        prompt = await client.get_prompt("one", {"x": "kept"}, progress_token="prompt")
        assert prompt.description == "rendered"
        assert prompt.messages[0]["content"] == {"type": "text", "text": "kept"}
        assert requests(server, "prompts/get")[0]["params"]["_meta"] == {
            "progressToken": "prompt"
        }
        await client.close()

    asyncio.run(drive())


def test_notifications_callbacks_and_sinks_are_recursively_redacted():
    async def drive():
        secret = "header-secret-4477"
        seen = {"tools": [], "resources": [], "updated": [], "prompts": [], "logs": [], "progress": []}
        server = SpecServer()
        client = await initialized_client(
            server,
            secrets=[secret],
            on_tools_changed=seen["tools"].append,
            on_resources_changed=seen["resources"].append,
            on_resource_updated=seen["updated"].append,
            on_prompts_changed=seen["prompts"].append,
            log_sink=seen["logs"].append,
            progress_sink=seen["progress"].append,
        )
        transport = server.connections[0]
        await transport.notify("notifications/tools/list_changed", {"why": secret})
        await transport.notify("notifications/resources/list_changed", {"why": secret})
        await transport.notify("notifications/resources/updated", {"uri": f"file:///{secret}"})
        await transport.notify("notifications/prompts/list_changed", {"why": secret})
        await transport.notify("notifications/message", {"data": {"token": secret}})
        await transport.notify(
            "notifications/progress", {"progressToken": "p", "message": secret}
        )
        await asyncio.sleep(0)

        assert seen["tools"] == [{"why": "[REDACTED]"}]
        assert seen["resources"] == [{"why": "[REDACTED]"}]
        assert seen["updated"] == [{"uri": "file:///[REDACTED]"}]
        assert seen["prompts"] == [{"why": "[REDACTED]"}]
        assert seen["logs"] == [{"data": {"token": "[REDACTED]"}}]
        assert seen["progress"] == [{"progressToken": "p", "message": "[REDACTED]"}]
        await client.close()

    asyncio.run(drive())


def test_capability_rejection_happens_without_sending_operations():
    async def drive():
        server = SpecServer(capabilities={"resources": {}})
        client = await initialized_client(server)

        with pytest.raises(CapabilityError, match="tools"):
            await client.list_tools()
        with pytest.raises(CapabilityError, match="subscriptions"):
            await client.subscribe_resource("file:///a")
        with pytest.raises(CapabilityError, match="prompts"):
            await client.get_prompt("one")
        assert not requests(server, "tools/list")
        assert not requests(server, "resources/subscribe")
        assert not requests(server, "prompts/get")
        await client.close()

    asyncio.run(drive())


def test_safe_operation_reconnects_reinitializes_and_retries():
    async def drive():
        server = SpecServer()
        client = await initialized_client(server)
        await server.connections[0].incoming.put(None)
        await asyncio.sleep(0)

        result = await client.read_resource("file:///a")
        assert result.contents[0]["text"] == "text"
        assert len(server.connections) == 2
        assert len(requests(server, "initialize")) == 2
        assert len(requests(server, "resources/read")) == 1
        await client.close()

    asyncio.run(drive())


def test_safe_operation_retries_when_transport_dies_during_request():
    async def drive():
        server = SpecServer()
        client = await initialized_client(server)
        server.die_on = (0, "resources/read")

        result = await client.read_resource("file:///a")
        assert result.contents[0]["text"] == "text"
        assert len(requests(server, "resources/read")) == 2
        await client.close()

    asyncio.run(drive())


def test_interrupted_tool_call_is_never_replayed_and_next_operation_recovers():
    async def drive():
        server = SpecServer()
        client = await initialized_client(server)
        server.die_on = (0, "tools/call")

        with pytest.raises(MCPClientError, match="EOF"):
            await client.call_tool("uncertain")
        assert len(requests(server, "tools/call")) == 1
        assert server.calls == 0

        assert [tool["name"] for tool in await client.list_tools()] == ["alpha", "beta"]
        assert len(server.connections) == 2
        assert len(requests(server, "tools/call")) == 1
        await client.close()

    asyncio.run(drive())


def test_reconnect_uses_new_capabilities_before_sending_operation():
    async def drive():
        server = SpecServer()
        client = await initialized_client(server)
        await server.connections[0].incoming.put(None)
        await asyncio.sleep(0)
        server.capabilities = {"resources": {}}

        with pytest.raises(CapabilityError, match="tools"):
            await client.list_tools()
        assert len(requests(server, "initialize")) == 2
        assert not requests(server, "tools/list")
        await client.close()

    asyncio.run(drive())


def test_reconnect_retries_factory_failures_with_bounded_backoff():
    async def drive():
        server = SpecServer()
        delays = []
        attempts = 0
        client = await initialized_client(server, reconnect_attempts=2)
        await server.connections[0].incoming.put(None)
        await asyncio.sleep(0)
        original_factory = server.factory

        def flaky_factory():
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise OSError("temporary connect failure")
            return original_factory()

        async def record_sleep(delay):
            delays.append(delay)

        client.transport_factory = flaky_factory
        client._reconnect_backoff = 0.25
        client._reconnect_backoff_max = 0.4
        original_sleep = asyncio.sleep
        asyncio.sleep = record_sleep
        try:
            assert (await client.read_resource("file:///a")).contents[0]["text"] == "text"
        finally:
            asyncio.sleep = original_sleep
        assert attempts == 3
        assert delays == [0.25, 0.4]
        await client.close()

    asyncio.run(drive())


def test_secrets_are_redacted_from_all_client_exception_paths():
    async def drive():
        secret = "environment-secret-9988"
        server = SpecServer()
        client = await initialized_client(server, secrets=[secret])
        server.die_on = (0, "resources/read")
        original_factory = server.factory

        attempts = 0

        def failing_factory():
            nonlocal attempts
            attempts += 1
            raise OSError(f"cannot connect with {secret}")

        client.transport_factory = failing_factory
        with pytest.raises(MCPClientError) as caught:
            await client.read_resource("file:///secret")
        assert attempts == client._reconnect_attempts
        assert secret not in str(caught.value)
        assert "[REDACTED]" in str(caught.value)

        client.transport_factory = original_factory
        await client.initialize()
        server.handle = lambda method, params: (
            {
                "protocolVersion": LATEST_PROTOCOL_VERSION,
                "capabilities": server.capabilities,
                "serverInfo": {"name": "spec-server", "version": "1"},
            }
            if method == "initialize"
            else {"tools": [], "nextCursor": secret}
        )
        with pytest.raises(ValueError) as malformed:
            await client.list_tools()
        assert secret not in str(malformed.value)
        assert "[REDACTED]" in str(malformed.value)

        server.handle = lambda method, params: (_ for _ in ()).throw(RuntimeError(secret))
        with pytest.raises(MCPClientError) as remote:
            await client.request("custom/secret")
        assert secret not in str(remote.value)
        assert "[REDACTED]" in str(remote.value)
        await client.close()

    asyncio.run(drive())
