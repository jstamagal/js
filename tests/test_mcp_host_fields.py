"""MCP notification fields retain remote data without replacing host identity."""

import asyncio
import io
import json

import pytest

from js import events, runtime
from js.mcp.host import MCPHost
from js.mcp_config import MCPConfiguration, MCPPolicy, MCPServer
from test_mcp_runtime import FakeClient


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
        client = await host._ensure_server(server)
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


def test_paranoid_reindex_retains_last_remote_refresh_error_until_success():
    class FlakyClient(FakeClient):
        fail = False

        async def list_tools(self, **kwargs):
            if self.fail:
                raise RuntimeError("refresh failed with sentinel-secret")
            return await super().list_tools(**kwargs)

    async def run():
        server = MCPServer(name="server", normalized_name="server", transport="stdio", command=("unused",),
                           env={"API_KEY": "sentinel-secret"})
        host = MCPHost(MCPConfiguration((server,), MCPPolicy()), client_factory=FlakyClient, paranoid_mode="off")
        assert await host.discover(source="server")
        client = host.clients[server.name]
        client.fail = True
        client.kwargs["on_tools_changed"]({})
        await host.refresh()
        assert "sentinel-secret" in host._server_errors[server.name]
        cached = dict(host.remote_tools)
        for mode in ("on", "off", "on"):
            host.paranoid_mode = mode
            assert server.name in host._server_errors
            assert "refresh failed" in host._server_errors[server.name]
            assert ("sentinel-secret" in host._server_errors[server.name]) is (mode == "off")
            assert host.remote_tools.keys() == cached.keys()
            found = await host.discover(source="server")
            assert any(entry.id.startswith("mcp:server-error:") for entry in found)
        host._dirty.clear()
        assert any(entry.id.startswith("mcp:server-error:") for entry in await host.discover(source="server"))
        client.kwargs["on_tools_changed"]({})
        client.fail = False
        await host.refresh()
        assert server.name not in host._server_errors
        assert server.name not in host._raw_server_errors
        await host.close()

    asyncio.run(run())


def test_telemetry_reserved_field_aliases_preserve_original_payload():
    stream = io.StringIO()
    host = MCPHost(MCPConfiguration((), MCPPolicy()),
                   telemetry=runtime.Telemetry(None, trace_sink=stream))
    payload = {"kind": "remote-kind", "self": "remote-self", "notification_kind": "remote-alias"}
    host._event("mcp_log", **payload)
    record = json.loads(stream.getvalue().removeprefix("FLIGHT "))
    assert record["kind"] == "mcp_log"
    assert record["notification_kind"] == "remote-kind"
    assert record["notification_self"] == "remote-self"
    assert record["notification_fields"] == payload
