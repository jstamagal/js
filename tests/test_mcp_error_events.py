"""Recoverable MCP server failures remain valid turn hook events."""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from js.events import EventHooks
from js.mcp.host import MCPHost
from js.mcp_config import MCPConfiguration, MCPPolicy, MCPServer
from js.turn_stream import TurnEvents


def test_discovery_with_broken_server_uses_real_turn_hooks(tmp_path):
    async def drive():
        observed = []
        hooks = EventHooks()
        turn = TurnEvents(
            hooks, lambda event, payload: observed.append((event, payload)),
            SimpleNamespace(event=lambda *args, **kwargs: None), model="test", provider_id=None,
        )
        fixture = Path(__file__).parent / "fixtures" / "fake_mcp_stdio_server.py"
        servers = (
            MCPServer("Healthy", "healthy", "stdio", command=sys.executable, args=(str(fixture),)),
            MCPServer("Broken", "broken", "stdio", command=str(tmp_path / "missing-server")),
        )
        host = MCPHost(MCPConfiguration(servers, MCPPolicy()), event_sink=turn.emit)
        try:
            found = await host.discover(query="mcp")
            assert any(entry.id == "mcp:healthy__alpha" for entry in found)
            assert any(entry.id == "mcp:server-error:broken" for entry in found)
            assert any(event == "mcp_server_error" and payload["server"] == "Broken"
                       for event, payload in observed)
            tool = next(tool for tool in host.tools({"healthy__alpha"}) if tool.name == "healthy__alpha")
            result = await tool.handler(text="still usable")
            assert not result.is_error
        finally:
            await host.close()

    asyncio.run(drive())


def test_mcp_server_error_hooks_are_supported_without_accepting_unknown_events():
    dispatched = []
    hooks = EventHooks(lambda hook, emission: dispatched.append(emission.payload))
    hooks.add("mcp_server_error", "record_failure")
    hooks.emit("mcp_server_error", server="Broken", error="unavailable")
    assert dispatched == [{"server": "Broken", "error": "unavailable"}]
    with pytest.raises(ValueError):
        hooks.emit("nonexistent_mcp_event")
