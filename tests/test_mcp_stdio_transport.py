from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import pytest

from js.mcp.client import MCPClient, MCPClientError
from js.mcp.transports import StdioTransport, StdioTransportError

FIXTURE = Path(__file__).parent / "fixtures" / "fake_mcp_stdio_server.py"
SECRET = "STDIO_SENTINEL_CREDENTIAL"


def factory(*extra: str, env: dict[str, str] | None = None, **kwargs):
    transports: list[StdioTransport] = []

    def create() -> StdioTransport:
        transport = StdioTransport(
            sys.executable,
            (str(FIXTURE), *extra),
            env=env,
            name="fake stdio",
            **kwargs,
        )
        transports.append(transport)
        return transport

    return create, transports


def run(coro):
    return asyncio.run(coro)


def test_initialize_paginated_tools_call_and_notification_over_real_pipes():
    async def scenario():
        changed = asyncio.Event()
        create, transports = factory()
        client = MCPClient(create, on_tools_changed=lambda _params: changed.set())
        result = await client.initialize()
        assert result.server_info.name == "fake-stdio"
        await asyncio.wait_for(changed.wait(), 1)
        tools = await client.list_tools()
        assert [tool["name"] for tool in tools] == ["alpha", "beta"]
        call = await client.call_tool("alpha", {"text": "hello"})
        assert call.content[0]["text"] == "hello"
        assert call.structured_content == {"called": "alpha"}
        await client.close()
        process = transports[0].process
        assert process is not None and process.returncode == 0

    run(scenario())


def test_abrupt_death_is_reconnected_through_client_factory(tmp_path):
    async def scenario():
        marker = tmp_path / "died-once"
        create, transports = factory("--mode", "die-once", "--state-file", str(marker))
        client = MCPClient(create, reconnect_backoff=0, reconnect_attempts=2)
        await client.initialize()
        tools = await client.list_tools()
        assert [tool["name"] for tool in tools] == ["alpha", "beta"]
        assert len(transports) == 2
        assert transports[0].process is not None
        assert transports[0].process.returncode == 23
        await client.close()
        assert transports[1].process is not None
        assert transports[1].process.returncode == 0

    run(scenario())


def test_cancellation_reaches_subprocess_and_connection_remains_usable():
    async def scenario():
        cancelled = asyncio.Event()
        create, transports = factory()
        client = MCPClient(
            create,
            log_sink=lambda params: cancelled.set() if params.get("data") == "cancelled" else None,
        )
        await client.initialize()
        with pytest.raises(MCPClientError, match="TimeoutError"):
            await client.request("test/slow", timeout=0.05)
        await asyncio.wait_for(cancelled.wait(), 1)
        assert [tool["name"] for tool in await client.list_tools()] == ["alpha", "beta"]
        await client.close()
        assert transports[0].process is not None
        assert transports[0].process.returncode == 0

    run(scenario())


def test_stderr_is_drained_without_leaking_credentials(caplog):
    async def scenario():
        create, transports = factory("--mode", "stderr", env={"MCP_SENTINEL": SECRET})
        client = MCPClient(create, secrets=(SECRET,))
        await asyncio.wait_for(client.initialize(), 2)
        assert [tool["name"] for tool in await client.list_tools()] == ["alpha", "beta"]
        await client.close()
        assert transports[0].process is not None
        assert transports[0].process.returncode is not None

    with caplog.at_level(logging.DEBUG):
        run(scenario())
    assert SECRET not in caplog.text


def test_non_protocol_stdout_is_safe_transport_error(caplog):
    async def scenario():
        create, transports = factory(
            "--mode",
            "noise",
            "--secret-arg",
            SECRET,
            env={"MCP_SENTINEL": SECRET},
        )
        client = MCPClient(create, secrets=(SECRET,), reconnect_attempts=0)
        with pytest.raises(MCPClientError) as caught:
            await client.initialize(timeout=1)
        assert "non-protocol data" in str(caught.value)
        assert SECRET not in str(caught.value)
        await client.close()
        assert transports[0].process is not None
        assert transports[0].process.returncode is not None

    with caplog.at_level(logging.DEBUG):
        run(scenario())
    assert SECRET not in caplog.text


def test_close_terminates_stubborn_child_with_bounded_fallback():
    async def scenario():
        transport = StdioTransport(
            sys.executable,
            (str(FIXTURE), "--mode", "stubborn"),
            close_timeout=0.02,
            terminate_timeout=0.2,
        )
        await transport.start()
        process = transport.process
        assert process is not None and process.returncode is None
        await transport.close()
        assert process.returncode is not None
        await transport.close()

    run(scenario())


def test_spawn_failure_does_not_expose_arguments_or_environment():
    async def scenario():
        transport = StdioTransport(
            "/definitely/missing/mcp-server",
            (SECRET,),
            env={"TOKEN": SECRET},
            name="configured server",
        )
        with pytest.raises(StdioTransportError) as caught:
            await transport.start()
        text = str(caught.value)
        assert "configured server" in text
        assert "/definitely/missing/mcp-server" in text
        assert SECRET not in text

    run(scenario())
