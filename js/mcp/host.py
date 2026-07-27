"""Lazy MCP host integration for one js turn/session."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from ..mcp_config import MCPConfiguration, MCPServer
from ..toolkit.core import CatalogEntry, Tool, ToolResult, compact_json
from .client import MCPClient
from .transports import StdioTransport, StreamableHTTPTransport

_NAME_RE = re.compile(r"[^a-z0-9_]+")


def normalize_tool_name(name: str) -> str:
    """Return a conservative model-safe component for a remote tool name."""
    return _NAME_RE.sub("_", str(name).strip().lower().replace("-", "_")).strip("_")


def transport_factory(server: MCPServer):
    """Build a fresh transport factory without leaking configured secrets."""
    if server.transport == "stdio":
        return lambda: StdioTransport(
            server.command or "", server.args, env=server.env, name=server.name
        )
    return lambda: StreamableHTTPTransport(
        server.url or "", headers=server.headers, name=server.name
    )


def _secret_values(server: MCPServer) -> tuple[str, ...]:
    values = [*server.env.values(), *server.headers.values()]
    return tuple(value for value in values if value)



def mcp_tool_result(result: Any) -> ToolResult:
    blocks = [dict(item) for item in result.content]
    if result.structured_content is not None:
        blocks.append({"type": "structured", "value": result.structured_content})
    return ToolResult(blocks=blocks, is_error=result.is_error)


def resource_result(result: Any) -> ToolResult:
    blocks: list[dict[str, Any]] = []
    for content in result.contents:
        item = dict(content)
        if "text" in item:
            blocks.append({"type": "text", "text": f"[{item.get('uri', '')}]\n{item['text']}"})
        elif "blob" in item:
            blocks.append({
                "type": "resource",
                "resource": {
                    "uri": item.get("uri", ""),
                    "mimeType": item.get("mimeType", "application/octet-stream"),
                    "blob": item["blob"],
                },
            })
    return ToolResult(blocks=blocks)


class MCPHost:
    """Own clients and lazy remote metadata for a single runtime invocation."""

    CONTROL_TOOLS = (
        ("mcp_resource_list", "List resources from an initialized MCP server."),
        ("mcp_resource_templates", "List resource templates from an initialized MCP server."),
        ("mcp_resource_read", "Read an MCP resource by URI."),
        ("mcp_resource_subscribe", "Subscribe to updates for an MCP resource."),
        ("mcp_resource_unsubscribe", "Unsubscribe from updates for an MCP resource."),
        ("mcp_prompt_list", "List prompts from an initialized MCP server."),
        ("mcp_prompt_get", "Get an MCP prompt and its messages."),
    )

    def __init__(
        self,
        config: MCPConfiguration,
        *,
        client_factory: Callable[..., MCPClient] = MCPClient,
        telemetry: Any = None,
        event_sink: Callable[..., Any] | None = None,
    ) -> None:
        self.config = config
        self._client_factory = client_factory
        self.telemetry = telemetry
        self.event_sink = event_sink
        self.clients: dict[str, MCPClient] = {}
        self.remote_tools: dict[str, tuple[str, str, dict[str, Any]]] = {}
        self.loaded: set[str] = set()
        self._dirty: set[str] = set()
        self._closed = False

    def initial_catalog(self) -> tuple[CatalogEntry, ...]:
        """Only generic controls are visible before an MCP-scoped discovery."""
        return tuple(
            CatalogEntry(f"mcp:{name}", name, description, "mcp", "mcp")
            for name, description in self.CONTROL_TOOLS
        )

    async def discover(self, query: str = "", source: str = "") -> tuple[CatalogEntry, ...]:
        terms = [term for term in str(query).casefold().split() if term != "mcp"]
        for server in self.config.servers:
            await self._ensure_server(server)
        await self.refresh()
        entries = list(self.initial_catalog())
        for public, (server_name, _remote, raw) in self.remote_tools.items():
            description = str(raw.get("description") or raw.get("title") or "MCP tool")[:240]
            entries.append(CatalogEntry(f"mcp:{public}", public, description, "mcp", server_name))
        result = []
        for entry in sorted(entries, key=lambda item: item.id):
            haystack = f"{entry.id} {entry.name} {entry.description} {entry.source}".casefold()
            if source and entry.source.casefold() != source.casefold():
                continue
            if terms and not all(term in haystack for term in terms):
                continue
            result.append(entry)
        return tuple(result)

    async def _ensure_server(self, server: MCPServer) -> MCPClient:
        client = self.clients.get(server.name)
        if client is not None:
            if not client.initialized:
                await client.initialize()
            return client

        def changed(_params: dict[str, Any], name: str = server.name) -> None:
            self._dirty.add(name)

        def resource_updated(params: dict[str, Any], name: str = server.name) -> None:
            self._event("mcp_resource_updated", server=name, **params)

        def log(params: dict[str, Any], name: str = server.name) -> None:
            self._event("mcp_log", server=name, **params)

        def progress(params: dict[str, Any], name: str = server.name) -> None:
            self._event("mcp_progress", server=name, **params)

        client = self._client_factory(
            transport_factory(server),
            on_tools_changed=changed,
            on_resources_changed=changed,
            on_prompts_changed=changed,
            on_resource_updated=resource_updated,
            log_sink=log,
            progress_sink=progress,
            secrets=_secret_values(server),
        )
        self.clients[server.name] = client
        await client.initialize()
        self._dirty.add(server.name)
        return client

    def _event(self, kind: str, **payload: Any) -> None:
        if self.telemetry is not None:
            self.telemetry.event(kind, **payload)
        if self.event_sink is not None:
            self.event_sink(kind, **payload)

    async def refresh(self) -> None:
        names = set(self._dirty)
        self._dirty.clear()
        for server_name in sorted(names):
            client = self.clients.get(server_name)
            if client is None:
                continue
            tools = await client.list_tools()
            replacement: dict[str, tuple[str, str, dict[str, Any]]] = {}
            collisions: set[str] = set()
            for raw in tools:
                remote = raw.get("name")
                if not isinstance(remote, str):
                    continue
                component = normalize_tool_name(remote)
                server = next(s for s in self.config.servers if s.name == server_name)
                public = f"{server.normalized_name}__{component}"
                if not component or not self.config.allows_tool(public) or public in collisions:
                    continue
                if public in replacement or (
                    public in self.remote_tools and self.remote_tools[public][0] != server_name
                ):
                    self._event("mcp_catalog_collision", tool=public)
                    replacement.pop(public, None)
                    self.remote_tools.pop(public, None)
                    collisions.add(public)
                    continue
                replacement[public] = (server_name, remote, dict(raw))
            self.remote_tools = {
                name: value for name, value in self.remote_tools.items() if value[0] != server_name
            }
            self.remote_tools.update(replacement)

    async def before_model_call(self) -> None:
        if self._dirty:
            await self.refresh()

    def load(self, item_id: str) -> list[str] | None:
        name = item_id.removeprefix("mcp:")
        if name in dict(self.CONTROL_TOOLS) or name in self.remote_tools:
            self.loaded.add(name)
            return [name]
        return None

    def tools(self) -> tuple[Tool, ...]:
        return tuple(self._make_tool(name) for name in sorted(self.loaded))

    def _make_tool(self, name: str) -> Tool:
        remote = self.remote_tools.get(name)
        if remote is not None:
            server, remote_name, raw = remote
            schema = raw.get("inputSchema") if isinstance(raw.get("inputSchema"), dict) else {}
            properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
            required = schema.get("required") if isinstance(schema.get("required"), list) else []

            async def call(context=None, **kwargs: Any) -> ToolResult:
                current = self.remote_tools.get(name)
                if current is None or not self.config.allows_tool(name):
                    return ToolResult.text(f"ERROR: MCP tool {name!r} is no longer available", is_error=True)
                client = self.clients[current[0]]
                return mcp_tool_result(await client.call_tool(current[1], kwargs))

            return Tool(name, str(raw.get("description") or "MCP tool"), call, properties, tuple(required))
        return self._control_tool(name)

    def _server(self, server: str) -> MCPClient:
        if server not in self.clients:
            raise ValueError(f"MCP server {server!r} is not initialized")
        return self.clients[server]

    def _control_tool(self, name: str) -> Tool:
        description = dict(self.CONTROL_TOOLS)[name]
        if name == "mcp_resource_list":
            async def handler(server: str, context=None):
                return compact_json(await self._server(server).list_resources())
            params = {"server": {"type": "string"}}
            required = ("server",)
        elif name == "mcp_resource_templates":
            async def handler(server: str, context=None):
                return compact_json(await self._server(server).list_resource_templates())
            params = {"server": {"type": "string"}}
            required = ("server",)
        elif name == "mcp_resource_read":
            async def handler(server: str, uri: str, context=None):
                return resource_result(await self._server(server).read_resource(uri))
            params = {"server": {"type": "string"}, "uri": {"type": "string"}}
            required = ("server", "uri")
        elif name in {"mcp_resource_subscribe", "mcp_resource_unsubscribe"}:
            async def handler(server: str, uri: str, context=None):
                client = self._server(server)
                method = client.subscribe_resource if name.endswith("subscribe") and not name.endswith("unsubscribe") else client.unsubscribe_resource
                await method(uri)
                return compact_json({"server": server, "uri": uri, "subscribed": method == client.subscribe_resource})
            params = {"server": {"type": "string"}, "uri": {"type": "string"}}
            required = ("server", "uri")
        elif name == "mcp_prompt_list":
            async def handler(server: str, context=None):
                return compact_json(await self._server(server).list_prompts())
            params = {"server": {"type": "string"}}
            required = ("server",)
        else:
            async def handler(server: str, name: str, arguments: dict | None = None, context=None):
                result = await self._server(server).get_prompt(name, arguments or None)
                return compact_json({"description": result.description, "messages": result.messages})
            params = {
                "server": {"type": "string"}, "name": {"type": "string"},
                "arguments": {"type": "object", "additionalProperties": {"type": "string"}},
            }
            required = ("server", "name")
        return Tool(name, description, handler, params, required)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        clients, self.clients = list(self.clients.values()), {}
        if clients:
            import asyncio
            await asyncio.gather(*(client.close() for client in clients), return_exceptions=True)
