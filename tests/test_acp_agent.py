"""ACP adapter behavior with a local, deterministic turn worker."""

import asyncio
import json
import os
import signal
import sys
from unittest.mock import AsyncMock

import pytest
from acp import RequestError, schema as acp

from js import acp_agent, paths


def test_resume_empty_session_with_fresh_mcp_configuration(tmp_path, monkeypatch):
    async def check():
        monkeypatch.setattr(paths, "sessions_root", lambda: tmp_path / "sessions")
        cwd = str(tmp_path)
        created = await acp_agent.Agent([]).new_session(cwd)
        assert created.config_options[0].category == "model"
        new_agent = acp_agent.Agent([])
        with pytest.raises(RequestError):
            await new_agent.resume_session("../invalid", cwd)
        wrong_path = tmp_path / "other"
        wrong_path.mkdir()
        with pytest.raises(RequestError):
            await new_agent.resume_session(created.session_id, str(wrong_path))
        resumed = await new_agent.resume_session(
            created.session_id, cwd,
            [acp.HttpMcpServer(name="tools", url="https://example.org/mcp", headers=[])],
        )
        assert resumed.config_options == created.config_options
        assert new_agent.sessions[created.session_id].mcp_servers == {
            "tools": {"url": "https://example.org/mcp", "headers": {}},
        }
    asyncio.run(check())


@pytest.mark.parametrize(
    ("response", "allowed"),
    [
        (acp.RequestPermissionResponse(outcome=acp.AllowedOutcome(option_id="allow")), True),
        (acp.RequestPermissionResponse(outcome=acp.AllowedOutcome(option_id="deny")), False),
        (acp.RequestPermissionResponse(outcome=acp.AllowedOutcome(option_id="unknown")), False),
        (acp.RequestPermissionResponse(outcome=acp.DeniedOutcome()), False),
        (None, False),
        (RequestError.method_not_found("session/request_permission"), False),
    ],
)
def test_permission_matches_call_and_uses_effective_arguments(response, allowed, tmp_path):
    async def check():
        connection = AsyncMock()
        if isinstance(response, Exception):
            connection.request_permission.side_effect = response
        else:
            connection.request_permission.return_value = response
        updates = acp_agent.TurnUpdates(connection, "session", tmp_path)
        await updates.emit({"type": "tool_call", "id": "call", "name": "shell", "arguments": {"command": "old"}})
        start = connection.session_update.await_args.kwargs["update"]
        reason = await updates.approve({"id": "call", "name": "shell", "arguments": '{"command": "new"}'})
        permission = connection.request_permission.await_args.kwargs["tool_call"]
        assert (reason is None) == allowed
        assert (permission.tool_call_id, permission.kind) == (start.tool_call_id, start.kind)
        assert permission.raw_input == {"command": "new"}
    asyncio.run(check())


@pytest.mark.parametrize(("name", "kind"), [
    ("patch", "edit"), ("pi_edit", "edit"), ("read", "read"), ("remove", "delete"),
])
def test_file_tool_location_and_id(tmp_path, name, kind):
    async def check():
        connection = AsyncMock()
        updates = acp_agent.TurnUpdates(connection, "session", tmp_path)
        await updates.emit({"type": "tool_call", "id": "call", "name": name,
                            "arguments": {"path": "before.txt"}})
        await updates.approve({"id": "call", "name": name, "arguments": '{"path":"after.txt"}'})
        await updates.emit({"type": "tool_result", "id": "call", "result": "done"})
        start, result = [call.kwargs["update"] for call in connection.session_update.await_args_list]
        permission = connection.request_permission.await_args.kwargs["tool_call"]
        assert start.tool_call_id == permission.tool_call_id == result.tool_call_id
        assert start.kind == permission.kind == kind
        assert start.raw_input == {"path": "before.txt"}
        assert permission.raw_input == {"path": "after.txt"}
        assert start.locations[0].path == str(tmp_path / "before.txt")
        assert permission.locations[0].path == str(tmp_path / "after.txt")
    asyncio.run(check())


@pytest.mark.parametrize("stop", ["cancel", "eof", "signal"])
def test_stdio_handshake_updates_and_permission_cancellation(tmp_path, stop):
    async def check():
        code = """
from js import acp_agent, acp_turn

async def worker(*, cwd, text, emit, approve, **kwargs):
    await emit({'type': 'reasoning', 'delta': 'thinking'})
    await emit({'type': 'text', 'delta': text})
    await emit({'type': 'tool_call', 'id': 'tool', 'name': 'shell'})
    try:
        result = await approve({'id': 'tool', 'name': 'shell', 'arguments': {'command': 'test'}})
    finally:
        (cwd / 'worker-cleaned-up').touch()
    await emit({'type': 'tool_result', 'id': 'tool', 'result': result or 'done', 'ok': not result})

acp_turn.run_turn = worker
raise SystemExit(acp_agent.run([]))
"""
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", code,
            env={**os.environ, "JS_HOME": str(tmp_path / "profile"), "JS_ACP_STEER_WAIT": "0"},
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        )

        async def send(method, params, request_id=None):
            message = {"jsonrpc": "2.0", "method": method, "params": params}
            if request_id is not None:
                message["id"] = request_id
            process.stdin.write(json.dumps(message).encode() + b"\n")
            await process.stdin.drain()

        async def receive():
            line = await asyncio.wait_for(process.stdout.readline(), 5)
            assert line
            return json.loads(line)

        try:
            await send("initialize", {"protocolVersion": 2}, 1)
            initialized = (await receive())["result"]
            assert initialized["protocolVersion"] == 1
            assert initialized["agentCapabilities"]["sessionCapabilities"]["resume"] == {}
            await send("session/new", {"cwd": str(tmp_path), "mcpServers": []}, 2)
            session = (await receive())["result"]["sessionId"]
            await send("session/resume", {"cwd": str(tmp_path), "sessionId": session, "mcpServers": []}, 3)
            assert "result" in await receive()
            await send("session/prompt", {"sessionId": session, "prompt": [{"type": "text", "text": "hi"}]}, 4)
            for update in ("agent_thought_chunk", "agent_message_chunk", "tool_call"):
                assert (await receive())["params"]["update"]["sessionUpdate"] == update
            assert (await receive())["method"] == "session/request_permission"
            if stop == "signal":
                process.terminate()
                assert await asyncio.wait_for(process.wait(), 5) == 128 + signal.SIGTERM
                assert (tmp_path / "worker-cleaned-up").is_file()
                return
            if stop == "eof":
                process.stdin.close()
                assert await asyncio.wait_for(process.wait(), 5) == 0
                assert (tmp_path / "worker-cleaned-up").is_file()
                return
            await send("session/prompt", {"sessionId": session, "prompt": [{"type": "text", "text": "hi"}]}, 5)
            assert (await receive())["error"]["code"] == -32600
            await send("session/cancel", {"sessionId": session})
            while (message := await receive()).get("id") != 4:
                if message.get("method") == "session/update":
                    assert message["params"]["update"]["status"] == "failed"
            assert message["result"]["stopReason"] == "cancelled"
            process.stdin.close()
            assert await asyncio.wait_for(process.wait(), 5) == 0
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    asyncio.run(check())
