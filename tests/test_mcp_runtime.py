from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace

import pytest

from js import model_client, runtime
from js.mcp.host import MCPHost
from js.mcp.types import CallToolResult, GetPromptResult, ReadResourceResult
from js.mcp_config import MCPConfiguration, MCPPolicy, MCPServer
from js.toolkit.core import ToolContext, ToolResult


class FakeClient:
    instances = []

    def __init__(self, _factory, **kwargs):
        self.kwargs = kwargs
        self.initialized = False
        self.closed = False
        self.tools = [
            {"name": "Read File", "description": "read remote", "inputSchema": {
                "type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]
            }},
            {"name": "read-file", "description": "collision", "inputSchema": {}},
            {"name": "denied", "description": "secret metadata", "inputSchema": {}},
        ]
        self.calls = []
        self.subscriptions = []
        type(self).instances.append(self)

    async def initialize(self):
        self.initialized = True

    async def list_tools(self):
        return self.tools

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return CallToolResult(content=[
            {"type": "text", "text": "hello"},
            {"type": "image", "data": base64.b64encode(b"png").decode(), "mimeType": "image/png"},
            {"type": "resource_link", "uri": "file:///x", "name": "x"},
            {"type": "resource", "resource": {"uri": "file:///y", "text": "inside"}},
        ], structured_content={"ok": True})

    async def list_resources(self):
        return [{"uri": "file:///x", "name": "x"}]

    async def list_resource_templates(self):
        return [{"uriTemplate": "file:///{name}", "name": "files"}]

    async def read_resource(self, uri):
        return ReadResourceResult(contents=[{"uri": uri, "text": "body"}])

    async def subscribe_resource(self, uri):
        self.subscriptions.append(("subscribe", uri))

    async def unsubscribe_resource(self, uri):
        self.subscriptions.append(("unsubscribe", uri))

    async def list_prompts(self):
        return [{"name": "review", "description": "Review"}]

    async def get_prompt(self, name, arguments):
        return GetPromptResult(description="Review", messages=[{
            "role": "user", "content": {"type": "text", "text": f"{name}:{arguments['file']}"}
        }])

    async def close(self):
        self.closed = True


@pytest.fixture
def config():
    server = MCPServer("Alpha Server", "alpha_server", "stdio", command="fake", env={"TOKEN": "SENTINEL"})
    return MCPConfiguration((server,), MCPPolicy(tool_deny=("alpha_server__denied",)))


def test_lazy_discovery_policy_collision_schema_call_and_shutdown(config):
    async def drive():
        FakeClient.instances.clear()
        host = MCPHost(config, client_factory=FakeClient)
        assert [entry.name for entry in host.initial_catalog()] == [name for name, _ in host.CONTROL_TOOLS]
        assert not FakeClient.instances
        found = await host.discover(query="mcp")
        assert FakeClient.instances[0].initialized
        assert "alpha_server__read_file" not in repr(found)
        assert "secret metadata" not in repr(found)

        client = FakeClient.instances[0]
        client.tools = [client.tools[0]]
        host._dirty.add("Alpha Server")
        await host.before_model_call()
        found = await host.discover()
        assert any(entry.name == "alpha_server__read_file" for entry in found)
        assert host.load("mcp:alpha_server__read_file") == ["alpha_server__read_file"]
        tool = host.tools()[0]
        assert tool.params["path"]["type"] == "string" and tool.required == ("path",)
        assert tool.openai_spec()["function"]["parameters"] == client.tools[0]["inputSchema"]
        result = await tool.handler(path="a")
        assert [block["type"] for block in result.blocks] == [
            "text", "image", "resource_link", "resource", "structured"
        ]
        assert client.calls == [("Read File", {"path": "a"})]
        client.tools[0]["inputSchema"]["properties"]["path"]["description"] = "refreshed"
        host._dirty.add("Alpha Server")
        await host.before_model_call()
        refreshed = next(tool for tool in host.tools() if tool.name == "alpha_server__read_file")
        assert refreshed.params["path"]["description"] == "refreshed"
        history = result.dehydrated()
        assert base64.b64encode(b"png").decode() not in history and "SENTINEL" not in history
        assert "inside" in history and '{"ok":true}' in history
        await host.close()
        assert client.closed

    asyncio.run(drive())


def test_resource_prompt_controls_notifications_and_redaction(config):
    async def drive():
        events = []
        telemetry = SimpleNamespace(event=lambda kind, **payload: events.append((kind, payload)))
        host = MCPHost(config, client_factory=FakeClient, telemetry=telemetry)
        await host.discover()
        client = FakeClient.instances[-1]
        calls = (
            ("mcp_resource_list", {"server": "Alpha Server"}),
            ("mcp_resource_templates", {"server": "Alpha Server"}),
            ("mcp_resource_read", {"server": "Alpha Server", "uri": "file:///x"}),
            ("mcp_resource_subscribe", {"server": "Alpha Server", "uri": "file:///x"}),
            ("mcp_resource_unsubscribe", {"server": "Alpha Server", "uri": "file:///x"}),
            ("mcp_prompt_list", {"server": "Alpha Server"}),
            ("mcp_prompt_get", {"server": "Alpha Server", "name": "review", "arguments": {"file": "a.py"}}),
        )
        for name, args in calls:
            host.load(f"mcp:{name}")
            value = await next(tool for tool in host.tools() if tool.name == name).handler(**args)
            assert json.loads(value) if isinstance(value, str) else isinstance(value, ToolResult)
        assert client.subscriptions == [("subscribe", "file:///x"), ("unsubscribe", "file:///x")]
        client.kwargs["log_sink"]({"level": "info", "data": "safe"})
        client.kwargs["progress_sink"]({"progressToken": 1, "progress": 0.5})
        client.kwargs["on_resource_updated"]({"uri": "file:///x"})
        assert [kind for kind, _ in events[-3:]] == ["mcp_log", "mcp_progress", "mcp_resource_updated"]
        assert "SENTINEL" not in repr(events)

    asyncio.run(drive())


def test_discovery_defensively_skips_denied_servers():
    async def drive():
        FakeClient.instances.clear()
        denied = MCPServer("Denied", "denied", "stdio", command="fake")
        host = MCPHost(
            MCPConfiguration((denied,), MCPPolicy(server_deny=("Denied",))),
            client_factory=FakeClient,
        )
        await host.discover(query="mcp")
        assert not FakeClient.instances

    asyncio.run(drive())


def test_prompt_get_preserves_media_content(config):
    async def drive():
        host = MCPHost(config, client_factory=FakeClient)
        await host.discover()
        client = FakeClient.instances[-1]

        async def get_prompt(_name, _arguments):
            return GetPromptResult(description="Media", messages=[
                {"role": "user", "content": {"type": "image", "data": "cG5n", "mimeType": "image/png"}},
                {"role": "assistant", "content": {"type": "text", "text": "done"}},
            ])

        client.get_prompt = get_prompt
        host.load("mcp:mcp_prompt_get")
        result = await next(tool for tool in host.tools() if tool.name == "mcp_prompt_get").handler(
            server="Alpha Server", name="media"
        )
        assert [block["type"] for block in result.blocks] == ["text", "text", "image", "text"]
        assert result.blocks[-1]["text"] == "[assistant]\ndone"

    asyncio.run(drive())


def test_async_dispatch_cancellation_reaches_handler():
    async def drive():
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def wait(context=None):
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        from js.toolkit.core import Tool
        from js.toolkit.registry import _registry_from_tools

        registry = _registry_from_tools((Tool("wait", "wait", wait, {}),))
        task = asyncio.create_task(runtime._dispatch_batch(
            [runtime._PendingToolCall("1", "wait", ["{}"])], runtime.Telemetry(debug_log=None), 0, False,
            runtime.ToolErrorTracker(), registry, ToolContext(), asyncio.get_running_loop(),
        ))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.is_set()

    asyncio.run(drive())


def test_structured_result_provider_media_and_dehydrated_history():
    result = ToolResult([
        {"type": "text", "text": "hello"},
        {"type": "image", "data": base64.b64encode(b"image-bytes").decode(), "mimeType": "image/png"},
        {"type": "resource", "resource": {"uri": "file:///x", "text": "embedded"}},
        {"type": "structured", "value": {"b": 2, "a": 1}},
    ])
    messages = model_client.build_tool_result_messages("tc", "remote", result)
    assert [message.role for message in messages] == ["tool", "user"]
    assert messages[1].parts[-1].data == b"image-bytes"
    history = runtime._history_tool_result_message(runtime._PendingToolCall("tc", "remote"), result)
    assert history[0]["content"] == 'hello\n[image image/png omitted from history]\n[embedded resource file:///x]\nembedded\n{"b":2,"a":1}'
    assert "image-bytes" not in repr(history)
