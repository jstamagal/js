"""MCP notification fields retain remote data without replacing host identity."""

import asyncio
import io
import json

import pytest
from test_mcp_runtime import FakeClient

from js import events, runtime
from js.mcp.host import MCPHost
from js.mcp.types import ServerCapabilities
from js.mcp_config import MCPConfiguration, MCPPolicy, MCPServer


@pytest.mark.parametrize("callback,event", [
    ("log_sink", "mcp_log"),
    ("progress_sink", "mcp_progress"),
    ("on_resource_updated", "mcp_resource_updated"),
])
def test_notification_server_and_kind_fields_are_delivered(callback, event):
    async def run():
        stream = io.StringIO()
        emitted = []
        hooks = events.EventHooks()

        def sink(event, **fields):
            emission = hooks.emit(event, **fields)
            emitted.append((emission.event, emission.payload))

        server = MCPServer(name="Configured Server", normalized_name="configured_server", transport="stdio", command=("unused",))
        host = MCPHost(MCPConfiguration((server,), MCPPolicy()), client_factory=FakeClient,
                       telemetry=runtime.Telemetry(None, trace_sink=stream),
                       event_sink=sink)
        await host.discover(source=server.name)
        client = host.clients[server.name]
        client.kwargs[callback]({"server": "remote override", "kind": "remote-kind", "data": "payload"})
        assert emitted[-1] == (event, {"server": server.name, "kind": "remote-kind", "data": "payload"})
        record = json.loads(stream.getvalue().splitlines()[-1].removeprefix("FLIGHT "))
        assert record["kind"] == event
        assert record["server"] == server.name
        assert record["notification_kind"] == "remote-kind"
        assert record["data"] == "payload"
        await host.close()

    asyncio.run(run())


@pytest.mark.parametrize("source", ["Configured Server ", " configured_server", " CONFIGURED_SERVER ", "\tConfigured Server\n"])
def test_discovery_source_whitespace_matches_exact_server(source):
    async def run():
        server = MCPServer(name="Configured Server", normalized_name="configured_server", transport="stdio", command=("unused",))
        host = MCPHost(MCPConfiguration((server,), MCPPolicy()), client_factory=FakeClient)
        expected = await host.discover(source="Configured Server")
        assert expected
        assert await host.discover(source=source) == expected
        assert all(entry.source == server.name for entry in expected)
        await host.close()

    asyncio.run(run())


def test_last_remote_refresh_error_is_redacted_and_kept_until_success():
    class FlakyClient(FakeClient):
        fail = False

        async def list_tools(self, **kwargs):
            if self.fail:
                raise RuntimeError("refresh failed with sentinel-secret")
            return await super().list_tools(**kwargs)

    async def run():
        server = MCPServer(name="server", normalized_name="server", transport="stdio", command=("unused",),
                           env={"API_KEY": "sentinel-secret"})
        host = MCPHost(MCPConfiguration((server,), MCPPolicy()), client_factory=FlakyClient)

        async def errors_shown():
            found = await host.discover(source="server")
            return [entry.description for entry in found if entry.id.startswith("mcp:server-error:")]

        assert await host.discover(source="server")
        client = host.clients[server.name]
        client.fail = True
        client.kwargs["on_tools_changed"]({})
        await host.refresh()
        cached = dict(host.remote_tools)
        for _ in range(2):
            shown = await errors_shown()
            assert host.remote_tools.keys() == cached.keys()
            assert len(shown) == 1
            assert "refresh failed" in shown[0]
            assert "sentinel-secret" not in shown[0]
        client.kwargs["on_tools_changed"]({})
        client.fail = False
        await host.refresh()
        assert await errors_shown() == []
        await host.close()

    asyncio.run(run())


def test_a_stored_error_is_redacted_and_a_narrowed_discovery_does_not_retry():
    """A server with no tools whose reconnect failed is not listed again by a
    discovery narrowed to another server; its stored error stays redacted."""
    class PromptsOnly(FakeClient):
        down = False

        async def initialize(self, *, timeout=None):
            if self.down:
                raise RuntimeError("reconnect failed with sentinel-secret")
            self.initialized = True

        @property
        def server_capabilities(self):
            return ServerCapabilities.from_wire({"prompts": {}})

    clients = iter((FakeClient, PromptsOnly))

    def client_factory(factory, **kwargs):
        return next(clients)(factory, **kwargs)

    async def run():
        web = MCPServer(name="web", normalized_name="web", transport="http", url="http://127.0.0.1:9/mcp")
        local = MCPServer(name="local", normalized_name="local", transport="stdio", command=("unused",),
                          env={"API_KEY": "sentinel-secret"})
        host = MCPHost(MCPConfiguration((web, local), MCPPolicy()), client_factory=client_factory)

        async def errors_shown(query=""):
            found = await host.discover(query=query)
            return [entry.description for entry in found if entry.id == "mcp:server-error:local"]

        assert await errors_shown() == []
        prompts_only = host.clients[local.name]
        prompts_only.initialized = False
        prompts_only.down = True
        assert ["sentinel-secret" in shown for shown in await errors_shown()] == [False]
        shown = await errors_shown("http error")
        assert len(shown) == 1
        assert "reconnect failed" in shown[0]
        assert "sentinel-secret" not in shown[0]
        await host.close()

    asyncio.run(run())


def test_telemetry_reserved_field_aliases_preserve_original_payload():
    stream = io.StringIO()
    server = MCPServer(name="server", normalized_name="server", transport="stdio", command=("unused",))
    host = MCPHost(MCPConfiguration((server,), MCPPolicy()), client_factory=FakeClient,
                   telemetry=runtime.Telemetry(None, trace_sink=stream))
    asyncio.run(host.discover(source=server.name))
    payload = {"kind": "remote-kind", "self": "remote-self", "notification_kind": "remote-alias"}
    host.clients[server.name].kwargs["log_sink"](payload)
    record = json.loads(stream.getvalue().splitlines()[-1].removeprefix("FLIGHT "))
    assert record["kind"] == "mcp_log"
    assert record["notification_kind"] == "remote-kind"
    assert record["notification_self"] == "remote-self"
    assert record["notification_fields"] == {**payload, "server": server.name}
