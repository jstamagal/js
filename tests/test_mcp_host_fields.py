"""MCP notification fields retain remote data without replacing host identity."""

import asyncio
import io
import json

import pytest
from test_mcp_runtime import FakeClient

from js import events, runtime
from js.mcp.host import MCPHost
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

        async def errors_shown():
            found = await host.discover(source="server")
            return [entry.description for entry in found if entry.id.startswith("mcp:server-error:")]

        assert await host.discover(source="server")
        client = host.clients[server.name]
        client.fail = True
        client.kwargs["on_tools_changed"]({})
        await host.refresh()
        assert ["sentinel-secret" in shown for shown in await errors_shown()] == [True]
        cached = dict(host.remote_tools)
        for mode in ("on", "off", "on"):
            host.paranoid_mode = mode
            assert host.remote_tools.keys() == cached.keys()
            shown = await errors_shown()
            assert len(shown) == 1
            assert "refresh failed" in shown[0]
            assert ("sentinel-secret" in shown[0]) is (mode == "off")
        host._dirty.clear()
        assert any(entry.id.startswith("mcp:server-error:") for entry in await host.discover(source="server"))
        client.kwargs["on_tools_changed"]({})
        client.fail = False
        await host.refresh()
        assert await errors_shown() == []
        assert server.name not in host._raw_server_errors
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
