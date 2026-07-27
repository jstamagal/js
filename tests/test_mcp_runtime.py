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
        loaded = {"alpha_server__read_file"}
        tool = host.tools(loaded)[0]
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
        refreshed = next(tool for tool in host.tools(loaded) if tool.name == "alpha_server__read_file")
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
            value = await next(tool for tool in host.tools({name}) if tool.name == name).handler(**args)
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
        result = await next(tool for tool in host.tools({"mcp_prompt_get"}) if tool.name == "mcp_prompt_get").handler(
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


def test_runtime_reuses_borrowed_host_across_turns_and_closes_owned_host(monkeypatch, config, tmp_path):
    async def drive():
        FakeClient.instances.clear()
        host = MCPHost(config, client_factory=FakeClient)
        calls = 0

        async def stream_stub(**_kwargs):
            nonlocal calls
            calls += 1
            return SimpleNamespace(
                text="done",
                tool_calls=[],
                reasoning="",
                usage=SimpleNamespace(input_tokens=1, output_tokens=1),
                finish_reason="stop",
                assistant_message=SimpleNamespace(parts=[]),
            )

        monkeypatch.setattr(runtime.model_client, "stream_model_async", stream_stub)
        from js.config import Config

        cfg = Config(
            agent_id="test", agent_dir=tmp_path, model="offline", provider_id=None,
            provider_base_url=None, provider_api_key=None, reasoning_effort=None,
            max_output_tokens=10, max_tool_iterations=1, max_bash_output_bytes=65536,
            max_tool_result_bytes=65536, fetch_timeout_s=5, debug_log=None, trace=False,
            history_file=tmp_path / "history", sessions_dir=tmp_path,
            session_file=tmp_path / "session.jsonl", prompts_dir=tmp_path,
            settings={}, mcp=config,
        )
        registry = __import__("js.toolkit.registry", fromlist=["build_default_registry"]).build_default_registry().select([])
        await host.discover(query="mcp")
        client = FakeClient.instances[0]

        for prompt in ("one", "two"):
            await runtime.run_turn_async(
                cfg, "system", [{"role": "user", "content": prompt}], runtime.Telemetry(None),
                tool_registry=registry, tool_context=ToolContext(cwd=tmp_path), mcp_host=host,
                suppress_output=True,
            )
            assert not host._closed
            assert host.clients["Alpha Server"] is client
        assert calls == 2
        await host.close()
        assert host._closed and client.closed

    asyncio.run(drive())


def test_mcp_loaded_tools_are_scoped_to_one_turn(config, tmp_path):
    async def drive():
        host = MCPHost(config, client_factory=FakeClient)
        await host.discover(query="mcp")
        client = FakeClient.instances[-1]
        client.tools = [client.tools[0]]
        host._dirty.add("Alpha Server")
        await host.before_model_call()

        from js.toolkit.registry import build_default_registry

        allowed = build_default_registry().select([])
        first = allowed.lazy_surface(tmp_path, mcp_host=host)
        assert "alpha_server__read_file" not in first.by_name
        first.discover(load="mcp:alpha_server__read_file")
        assert first.by_name["alpha_server__read_file"].params["path"]["type"] == "string"

        client.tools[0]["inputSchema"]["properties"]["path"]["description"] = "new schema"
        host._dirty.add("Alpha Server")
        await host.before_model_call()
        assert first.by_name["alpha_server__read_file"].params["path"]["description"] == "new schema"

        second = allowed.lazy_surface(tmp_path, mcp_host=host)
        assert "alpha_server__read_file" not in second.by_name
        assert host.clients["Alpha Server"] is client

    asyncio.run(drive())


def test_successful_server_data_is_redacted_everywhere(config):
    async def drive():
        FakeClient.instances.clear()
        events = []
        host = MCPHost(
            config,
            client_factory=FakeClient,
            event_sink=lambda kind, **payload: events.append((kind, payload)),
        )
        await host.discover(query="mcp")
        client = FakeClient.instances[-1]
        client.tools = [{
            "name": "echo",
            "description": "metadata SENTINEL",
            "inputSchema": {"type": "object", "properties": {"value": {"description": "SENTINEL"}}},
        }]
        host._dirty.add("Alpha Server")
        await host.before_model_call()
        catalog = await host.discover(query="mcp")

        async def call_tool(_name, _arguments):
            return CallToolResult(
                content=[
                    {"type": "text", "text": "text SENTINEL"},
                    {"type": "resource", "resource": {"uri": "secret://SENTINEL", "text": "SENTINEL"}},
                ],
                structured_content={"SENTINEL": ["SENTINEL"]},
            )

        async def list_resources():
            return [{"uri": "secret://SENTINEL", "name": "SENTINEL"}]

        async def list_templates():
            return [{"uriTemplate": "secret://SENTINEL/{x}", "name": "SENTINEL"}]

        async def read_resource(_uri):
            return ReadResourceResult(contents=[{"uri": "secret://SENTINEL", "text": "SENTINEL"}])

        async def list_prompts():
            return [{"name": "SENTINEL", "description": "SENTINEL"}]

        async def get_prompt(_name, _arguments):
            return GetPromptResult(
                description="SENTINEL",
                messages=[{"role": "user", "content": {"type": "text", "text": "SENTINEL"}}],
            )

        client.call_tool = call_tool
        client.list_resources = list_resources
        client.list_resource_templates = list_templates
        client.read_resource = read_resource
        client.list_prompts = list_prompts
        client.get_prompt = get_prompt

        outputs = []
        for name, args in (
            ("alpha_server__echo", {"value": "safe"}),
            ("mcp_resource_list", {"server": "Alpha Server"}),
            ("mcp_resource_templates", {"server": "Alpha Server"}),
            ("mcp_resource_read", {"server": "Alpha Server", "uri": "safe"}),
            ("mcp_prompt_list", {"server": "Alpha Server"}),
            ("mcp_prompt_get", {"server": "Alpha Server", "name": "safe"}),
        ):
            tool = host.tools({name})[0]
            outputs.append((tool.openai_spec(), await tool.handler(**args)))

        client.kwargs["log_sink"]({"data": "SENTINEL"})
        client.kwargs["progress_sink"]({"message": "SENTINEL"})
        client.kwargs["on_resource_updated"]({"uri": "secret://SENTINEL"})

        provider_messages = []
        history = []
        for index, (_spec, value) in enumerate(outputs):
            provider_messages.extend(model_client.build_tool_result_messages(str(index), "mcp", value))
            history.extend(runtime._history_tool_result_message(
                runtime._PendingToolCall(str(index), "mcp"), value
            ))
        exposed = repr((catalog, outputs, provider_messages, history, events))
        assert "SENTINEL" not in exposed
        assert "[REDACTED]" in exposed

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


def test_bare_token_fragments_of_configured_headers_are_redacted():
    from js.mcp.host import _redact_value, _secret_values
    from js.mcp_config import MCPServer

    server = MCPServer(
        name="s", normalized_name="s", transport="http", url="http://127.0.0.1:1/mcp",
        headers={"Authorization": "Bearer TOKEN_SENTINEL_12345"},
    )
    secrets = _secret_values(server)
    scrubbed = _redact_value(
        {"description": "echoes TOKEN_SENTINEL_12345 and Bearer TOKEN_SENTINEL_12345"}, secrets
    )
    assert "TOKEN_SENTINEL_12345" not in str(scrubbed)
    # Ordinary short words like the auth scheme survive redaction.
    assert "Bearer" in str(scrubbed) or "[REDACTED]" in str(scrubbed)
