from __future__ import annotations

import asyncio
import json
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import ai
import ai.types.messages
import ai.types.usage

from js import config as config_mod
from js import context_budget, memory, runtime, settings
from js.mcp.client import MCPClientError
from js.mcp.host import MCPHost
from js.model_client import ModelStreamResult, ModelToolCall
from js.toolkit import ToolContext
from js.toolkit.registry import build_default_registry

FIXTURE = Path(__file__).parent / "fixtures" / "fake_mcp_stdio_server.py"
SECRET = "MCP_E2E_SENTINEL_9482"


class HTTPState:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.deleted = threading.Event()


class MCPHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def state(self) -> HTTPState:
        return self.server.state

    def log_message(self, _format, *_args):
        pass

    def do_GET(self):
        self.send_response(405)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_DELETE(self):
        self.state.deleted.set()
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.state.requests.append(body)
        method = body.get("method")
        params = body.get("params", {})
        if "id" not in body:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        results = {
            "initialize": {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
                "serverInfo": {"name": "fake-http", "version": "1"},
            },
            "tools/list": {
                "tools": [{
                    "name": "inspect",
                    "description": "Inspect over HTTP " + SECRET,
                    "inputSchema": {
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                        "required": ["value"],
                    },
                }]
            },
            "resources/read": {
                "contents": [{"uri": params.get("uri"), "text": "http resource " + SECRET}]
            },
            "prompts/get": {
                "description": "HTTP prompt " + SECRET,
                "messages": [{
                    "role": "user",
                    "content": {"type": "text", "text": "review:" + str(params.get("arguments", {}).get("file"))},
                }],
            },
        }
        if method == "tools/call":
            result = {
                "content": [{"type": "text", "text": str(params.get("arguments", {}).get("value"))}],
                "structuredContent": {"called": params.get("name"), "transport": "http"},
            }
        else:
            result = results.get(method, {})
        encoded = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Mcp-Session-Id", "e2e-session")
        self.end_headers()
        self.wfile.write(encoded)


@contextmanager
def http_server():
    state = HTTPState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), MCPHandler)
    server.state = state
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, f"http://127.0.0.1:{server.server_port}/mcp"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def _isolate(monkeypatch, tmp_path: Path) -> Path:
    config_home = tmp_path / "config"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.chdir(project)
    for spec in settings.REGISTRY:
        if spec.env:
            monkeypatch.delenv(spec.env, raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    (config_home / "js").mkdir(parents=True)
    return config_home / "js" / "jsrc"


def _result(name: str | None = None, arguments: dict | None = None, *, text: str = "") -> ModelStreamResult:
    calls = [] if name is None else [ModelToolCall("call", name, json.dumps(arguments or {}))]
    parts = [
        ai.types.messages.ToolCallPart(tool_call_id=call.id, tool_name=call.name, tool_args=call.arguments)
        for call in calls
    ] or [ai.types.messages.TextPart(text=text)]
    return ModelStreamResult(
        text=text,
        tool_calls=calls,
        reasoning="",
        usage=ai.types.usage.Usage(input_tokens=10, output_tokens=1),
        finish_reason="tool_calls" if calls else "stop",
        assistant_message=ai.types.messages.Message(role="assistant", parts=parts),
    )


def test_configured_hosts_run_over_both_real_transports_and_reload_session(monkeypatch, tmp_path, capsys):
    async def scenario(url: str, state: HTTPState):
        jsrc = _isolate(monkeypatch, tmp_path)
        call_log = tmp_path / "stdio-calls.jsonl"
        servers = {
            "Local Pipe": {
                "command": sys.executable,
                "args": [str(FIXTURE), "--secret-arg", SECRET, "--call-log", str(call_log)],
            },
            "Loopback HTTP": {"url": url, "headers": {"Authorization": "Bearer " + SECRET}},
        }
        policy = {"e2e": {"servers": {"allow": ["*"]}, "tools": {"allow": ["*"]}}}
        jsrc.write_text(
            "set model.id offline-test\n"
            f"set mcp.servers {json.dumps(servers, separators=(',', ':'))}\n"
            f"set mcp.agents {json.dumps(policy, separators=(',', ':'))}\n"
            "set limits.max_tool_iterations 12\n"
            "set model.max_output_tokens 10\n",
            encoding="utf-8",
        )
        cfg = config_mod.from_env(agent_id="e2e")
        messages = [{"role": "user", "content": "exercise configured MCP hosts"}]
        memory.append_message(cfg.session_file, messages[0])
        emitted: list[list[dict]] = []
        scripted = iter([
            _result("tool_discovery", {"query": "mcp"}),
            _result("tool_discovery", {"load": "mcp:local_pipe__alpha"}),
            _result("local_pipe__alpha", {"text": "stdio hello"}),
            _result("tool_discovery", {"load": "mcp:loopback_http__inspect"}),
            _result("loopback_http__inspect", {"value": "http hello"}),
            _result("tool_discovery", {"load": "mcp:mcp_resource_read"}),
            _result("mcp_resource_read", {"server": "Loopback HTTP", "uri": "test://fixture"}),
            _result("tool_discovery", {"load": "mcp:mcp_prompt_get"}),
            _result("mcp_prompt_get", {"server": "Loopback HTTP", "name": "review", "arguments": {"file": "a.py"}}),
            _result(text="done"),
        ])

        async def fake_model(**kwargs):
            emitted.append([tool for tool in (kwargs["tools"] or [])])
            return next(scripted)

        monkeypatch.setattr(runtime.model_client, "stream_model_async", fake_model)
        await runtime.run_turn_async(
            cfg,
            "system",
            messages,
            runtime.Telemetry(None),
            tool_registry=build_default_registry().select([]),
            tool_context=ToolContext(cwd=tmp_path),
            suppress_output=True,
        )
        for message in messages[1:]:
            memory.append_message(cfg.session_file, message)

        assert state.deleted.wait(1)
        assert [json.loads(line)["name"] for line in call_log.read_text().splitlines()] == ["alpha"]
        methods = [request.get("method") for request in state.requests]
        assert "tools/list" in methods and "tools/call" in methods
        assert "resources/read" in methods and "prompts/get" in methods
        assert any(
            tool.name == "local_pipe__alpha"
            and tool.spec.params["properties"]["text"]["description"].startswith("refreshed")
            for tool in emitted[3]
        )
        tool_results = [message["content"] for message in messages if message.get("role") == "tool"]
        assert any('"transport":"http"' in result for result in tool_results)
        assert any("[REDACTED]" in result and "http resource" in result for result in tool_results)
        assert any("review:a.py" in result for result in tool_results)
        assert memory.load_messages(cfg.session_file) == messages
        exposed = repr((messages, emitted, state.requests, capsys.readouterr())) + cfg.session_file.read_text()
        assert SECRET not in exposed

    with http_server() as (state, url):
        asyncio.run(scenario(url, state))


def test_transport_death_is_not_replayed_and_next_safe_request_reconnects(tmp_path):
    async def scenario():
        marker = tmp_path / "died"
        call_log = tmp_path / "calls"
        server = {
            "Dying Pipe": {
                "command": sys.executable,
                "args": [
                    str(FIXTURE), "--mode", "die-call-once", "--state-file", str(marker),
                    "--call-log", str(call_log),
                ],
            }
        }
        from js.mcp_config import resolve

        host = MCPHost(resolve({"mcp": {"servers": server}}, "default"))
        await host.discover(query="mcp")
        tool = host.tools({"dying_pipe__alpha"})[0]
        try:
            await tool.handler(text="once")
        except MCPClientError:
            pass
        else:
            raise AssertionError("transport death should fail the non-replayable call")
        assert len(call_log.read_text().splitlines()) == 1
        assert [item["name"] for item in await host.clients["Dying Pipe"].list_tools()] == ["alpha", "beta"]
        assert len(call_log.read_text().splitlines()) == 1
        await host.close()

    asyncio.run(scenario())


def test_many_configured_servers_cost_only_discovery_until_remote_schema_is_loaded(monkeypatch, tmp_path):
    async def scenario():
        jsrc = _isolate(monkeypatch, tmp_path)
        start_log = tmp_path / "starts"
        servers = {
            f"Large {index}": {
                "command": sys.executable,
                "args": [str(FIXTURE), "--schema-bytes", "12000", "--start-log", str(start_log)],
            }
            for index in range(24)
        }
        jsrc.write_text(
            "set model.id offline-test\n"
            f"set mcp.servers {json.dumps(servers, separators=(',', ':'))}\n",
            encoding="utf-8",
        )
        cfg = config_mod.from_env(agent_id="budget", save_session=False)
        host = MCPHost(cfg.mcp)
        surface = build_default_registry().select([]).lazy_surface(tmp_path, mcp_host=host)
        initial = surface.openai_specs()
        initial_tokens = context_budget.estimate_tools_tokens(initial)
        assert [item["function"]["name"] for item in initial] == ["tool_discovery"]
        assert not start_log.exists()

        await surface.discover_async(query="mcp", source="large_7")
        assert start_log.read_text().splitlines() == ["started"]
        assert context_budget.estimate_tools_tokens(surface.openai_specs()) == initial_tokens
        surface.discover(load="mcp:large_7__alpha")
        loaded = surface.openai_specs()
        remote = next(item for item in loaded if item["function"]["name"] == "large_7__alpha")
        assert len(remote["function"]["parameters"]["properties"]["text"]["description"]) == 12000
        assert context_budget.estimate_tools_tokens(loaded) > initial_tokens + 3000
        assert context_budget.estimate_tools_tokens(loaded) == context_budget.estimate_tools_tokens([*initial, remote])
        await host.close()

    asyncio.run(scenario())
