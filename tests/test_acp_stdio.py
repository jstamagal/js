"""Exercise the ACP CLI and worker with a loopback streaming model."""

import asyncio
import json
import os
import sys
import threading
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


@pytest.fixture
def acp_provider():
    requests = []
    tool_call = {
        "index": 0,
        "id": "call_workspace_read",
        "type": "function",
        "function": {"name": "read", "arguments": '{"file_path":"workspace-note.txt"}'},
    }
    replies = [
        ({"tool_calls": [tool_call]}, "tool_calls"),
        ({"content": "First reply."}, "stop"),
        ({"content": "Second reply."}, "stop"),
    ]

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, body))
            if len(requests) > len(replies):
                self.send_error(400, "Unexpected model request")
                return
            delta, finish = replies[len(requests) - 1]
            frames = [
                {"index": 0, "delta": {"role": "assistant", **delta}, "finish_reason": None},
                {"index": 0, "delta": {}, "finish_reason": finish},
            ]
            chunks = [
                {
                    "id": "chatcmpl-acp",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "acp-fixture",
                    "choices": [frame],
                }
                for frame in frames
            ]
            payload = ("".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
                       + "data: [DONE]\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/v1", requests
        finally:
            server.shutdown()
            thread.join(timeout=5)


def test_acp_stdio_t3_session(tmp_path, acp_provider):
    base_url, requests = acp_provider
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = "Workspace file contents for the ACP regression."
    (workspace / "workspace-note.txt").write_text(marker)
    home = tmp_path / "profile"
    agent = home / "agents" / "defaultagent"
    agent.mkdir(parents=True)
    (agent / "agent.yaml").write_text("tools:\n  - read:eager\n")
    (agent / "prompt.md").write_text("Read the requested file.")
    env = {key: value for key, value in os.environ.items() if not key.startswith("JS_")}
    env.update(
        JS_HOME=str(home), JS_PROVIDER="llama.cpp", JS_BASE_URL=base_url,
        JS_API_KEY="fixture", JS_MODEL="acp-fixture", NO_PROXY="127.0.0.1",
    )
    stderr_path = tmp_path / "acp.stderr"
    first_prompt = "Read workspace-note.txt."
    second_prompt = "What did I ask on the previous turn?"

    @asynccontextmanager
    async def running_agent():
        with stderr_path.open("ab") as stderr:
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-I", "-m", "js", "--acp", cwd=tmp_path,
                env=env, stderr=stderr, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
            )

        try:
            yield process
            process.stdin.close()
            assert await asyncio.wait_for(process.stdout.read(), timeout=10) == b""
            assert await asyncio.wait_for(process.wait(), timeout=10) == 0
        finally:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await asyncio.wait_for(process.communicate(), timeout=10)

    async def drive():
        permitted_tool_calls = []

        async def request(process, request_id, method, params):
            message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            process.stdin.write((json.dumps(message) + "\n").encode())
            await process.stdin.drain()
            updates = []
            async with asyncio.timeout(30):
                while True:
                    line = await process.stdout.readline()
                    assert line, f"ACP closed stdout during {method}"
                    message = json.loads(line)
                    assert message["jsonrpc"] == "2.0"
                    if message.get("method") == "session/request_permission":
                        permission = message["params"]
                        assert permission["sessionId"] == params["sessionId"]
                        option = next(
                            item for item in permission["options"] if item["kind"] == "allow_once"
                        )
                        permitted_tool_calls.append(permission["toolCall"]["toolCallId"])
                        response = {
                            "jsonrpc": "2.0", "id": message["id"],
                            "result": {"outcome": {
                                "outcome": "selected", "optionId": option["optionId"],
                            }},
                        }
                        process.stdin.write((json.dumps(response) + "\n").encode())
                        await process.stdin.drain()
                        continue
                    if "id" in message:
                        assert message["id"] == request_id
                        assert "result" in message, message
                        return message["result"], updates
                    assert message["method"] == "session/update"
                    assert message["params"]["sessionId"] == params["sessionId"]
                    updates.append(message["params"]["update"])

        async def prompt(process, request_id, session_id, text, expected):
            result, updates = await request(process, request_id, "session/prompt", {
                "sessionId": session_id, "prompt": [{"type": "text", "text": text}],
            })
            assert result["stopReason"] == "end_turn"
            assert "".join(
                update["content"]["text"] for update in updates
                if update["sessionUpdate"] == "agent_message_chunk"
            ) == expected
            return updates

        async def initialize(process):
            result, _ = await request(process, 1, "initialize", {
                "protocolVersion": 2, "clientCapabilities": {},
                "clientInfo": {"name": "t3-code", "version": "test"},
            })
            assert result["protocolVersion"] == 1

        session_id = None
        try:
            async with running_agent() as process:
                await initialize(process)
                result, _ = await request(process, 2, "session/new", {
                    "cwd": str(workspace), "mcpServers": [],
                })
                session_id = result["sessionId"]
                updates = await prompt(process, 3, session_id, first_prompt, "First reply.")
                call = next(update for update in updates if update["sessionUpdate"] == "tool_call")
                assert any(
                    update["sessionUpdate"] == "tool_call_update"
                    and update["toolCallId"] == call["toolCallId"]
                    and update.get("status") == "completed"
                    for update in updates
                ), updates
                assert permitted_tool_calls == [call["toolCallId"]]
            async with running_agent() as process:
                await initialize(process)
                await request(process, 2, "session/resume", {
                    "sessionId": session_id, "cwd": str(workspace), "mcpServers": [],
                })
                await prompt(process, 3, session_id, second_prompt, "Second reply.")
        except (AssertionError, ValueError, OSError, TimeoutError) as exc:
            pytest.fail(f"ACP workflow failed: {exc!r}\n{stderr_path.read_text(errors='replace')}")

    asyncio.run(drive())

    assert [path for path, _ in requests] == ["/v1/chat/completions"] * 3
    first, tool_followup, second = (body for _, body in requests)
    assert any(tool["function"]["name"] == "read" for tool in first["tools"])
    result = next(message for message in tool_followup["messages"] if message["role"] == "tool")
    assert result["tool_call_id"] == "call_workspace_read"
    assert marker in result["content"]
    history = [(message["role"], message.get("content")) for message in second["messages"]]
    assert history.index(("user", first_prompt)) < history.index(("assistant", "First reply."))
    assert history[-1] == ("user", second_prompt)
    assert any(role == "tool" and marker in content for role, content in history)


def test_a_prompt_carrying_a_large_image_is_read(tmp_path):
    env = {key: value for key, value in os.environ.items() if not key.startswith("JS_")}
    env.update(JS_HOME=str(tmp_path / "profile"))
    image = "A" * (7 * 1024 * 1024)  # the base64 of an image of about 5 MiB

    async def drive():
        with (tmp_path / "acp.stderr").open("ab") as stderr:
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-I", "-m", "js", "--acp", cwd=tmp_path,
                env=env, stderr=stderr, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, limit=64 * 1024 * 1024,
            )
        try:
            async def request(request_id, method, params):
                message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
                process.stdin.write((json.dumps(message) + "\n").encode())
                await process.stdin.drain()
                async with asyncio.timeout(30):
                    while True:
                        line = await process.stdout.readline()
                        assert line, f"ACP closed stdout during {method}"
                        answer = json.loads(line)
                        if answer.get("id") == request_id:
                            return answer

            assert "result" in await request(1, "initialize", {
                "protocolVersion": 2, "clientCapabilities": {},
                "clientInfo": {"name": "t3-code", "version": "test"},
            })
            # No such session: the answer shows the whole line was read.
            answer = await request(2, "session/prompt", {
                "sessionId": "0" * 32,
                "prompt": [{"type": "image", "data": image, "mimeType": "image/png"}],
            })
            assert "error" in answer
        finally:
            process.stdin.close()
            if process.returncode is None:
                process.kill()
            await asyncio.wait_for(process.communicate(), timeout=10)

    asyncio.run(drive())
