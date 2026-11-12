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
