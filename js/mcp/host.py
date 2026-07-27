"""Lazy MCP host integration for one js turn/session."""

from __future__ import annotations

import asyncio
import inspect
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
    secrets = {value for value in (*server.env.values(), *server.headers.values()) if value}
    authorization_schemes = {
        "apikey", "basic", "bearer", "digest", "hoba", "mutual", "negotiate", "scram", "vapid",
    }
    for value in (*server.env.values(), *server.headers.values()):
        if not value:
            continue
        scheme, separator, credential = value.strip().partition(" ")
        if separator and credential and scheme.casefold() in authorization_schemes:
            # Servers commonly echo only the credential. Redact it even when it
            # is short, but never treat the ordinary auth scheme word as secret.
            secrets.add(credential)
    # Longest first so full values redact before their own fragments.
    return tuple(sorted(secrets, key=len, reverse=True))


def _redact_value(value: Any, secrets: tuple[str, ...]) -> Any:
    """Recursively scrub configured credentials from server-controlled data."""
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, dict):
        return {
            _redact_value(key, secrets) if isinstance(key, str) else key: _redact_value(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(item, secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(item, secrets) for item in value)
    return value


def mcp_tool_result(result: Any, secrets: tuple[str, ...] = ()) -> ToolResult:
    blocks = [_redact_value(dict(item), secrets) for item in result.content]
    if result.structured_content is not None:
        blocks.append({"type": "structured", "value": _redact_value(result.structured_content, secrets)})
    return ToolResult(blocks=blocks, is_error=result.is_error)


def resource_result(result: Any, secrets: tuple[str, ...] = ()) -> ToolResult:
    blocks: list[dict[str, Any]] = []
    for content in result.contents:
        item = _redact_value(dict(content), secrets)
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


def prompt_result(result: Any, secrets: tuple[str, ...] = ()) -> ToolResult:
    """Preserve prompt media while rendering roles in a deterministic text form."""
    blocks: list[dict[str, Any]] = []
    description = _redact_value(result.description, secrets)
    if description:
        blocks.append({"type": "text", "text": f"[description]\n{description}"})
    for raw_message in result.messages:
        message = _redact_value(raw_message, secrets)
        role = str(message.get("role", "user"))
        content = message.get("content")
        if not isinstance(content, dict):
            blocks.append({"type": "text", "text": f"[{role}]\n{compact_json(content)}"})
            continue
        item = dict(content)
        if item.get("type") == "text":
            blocks.append({"type": "text", "text": f"[{role}]\n{item.get('text', '')}"})
        else:
            blocks.append({"type": "text", "text": f"[{role}]"})
            blocks.append(item)
    return ToolResult(blocks=blocks)


class MCPHost:
    """Own clients and lazy remote metadata for one js session."""

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
        self._server_tools: dict[str, dict[str, tuple[str, str, dict[str, Any]]]] = {}
        self._dirty: dict[str, set[str]] = {}
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
            if self.config.policy.allows_server(server.name):
                await self._ensure_server(server)
        await self.refresh()
        entries = list(self.initial_catalog())
        for server_name, client in self.clients.items():
            capabilities = getattr(client, "server_capabilities", None)
            advertised = (
                sorted(name for name in ("tools", "resources", "prompts") if capabilities.supports(name))
                if capabilities is not None
                else []
            )
            detail = ", ".join(advertised) or "connected"
            entries.append(CatalogEntry(
                f"mcp:server:{normalize_tool_name(server_name)}",
                server_name,
                f"Connected MCP server ({detail}); use this exact server name with MCP controls.",
                "mcp",
                server_name,
            ))
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

        def changed(feature: str, name: str = server.name) -> None:
            self._dirty.setdefault(name, set()).add(feature)

        secrets = _secret_values(server)

        def resource_updated(params: dict[str, Any], name: str = server.name) -> None:
            self._event("mcp_resource_updated", server=name, **_redact_value(params, secrets))

        def log(params: dict[str, Any], name: str = server.name) -> None:
            self._event("mcp_log", server=name, **_redact_value(params, secrets))

        def progress(params: dict[str, Any], name: str = server.name) -> None:
            self._event("mcp_progress", server=name, **_redact_value(params, secrets))

        client = self._client_factory(
            transport_factory(server),
            on_tools_changed=lambda _params: changed("tools"),
            on_resources_changed=lambda _params: changed("resources"),
            on_prompts_changed=lambda _params: changed("prompts"),
            on_resource_updated=resource_updated,
            log_sink=log,
            progress_sink=progress,
            secrets=secrets,
        )
        self.clients[server.name] = client
        await client.initialize()
        capabilities = getattr(client, "server_capabilities", None)
        features = {
            name for name in ("tools", "resources", "prompts")
            if capabilities is None or capabilities.supports(name)
        }
        self._dirty.setdefault(server.name, set()).update(features)
        return client

    def _event(self, kind: str, **payload: Any) -> None:
        if self.telemetry is not None:
            self.telemetry.event(kind, **payload)
        if self.event_sink is not None:
            result = self.event_sink(kind, **payload)
            if inspect.isawaitable(result):
                asyncio.create_task(result)

    async def refresh(self) -> None:
        dirty, self._dirty = self._dirty, {}
        for server_name in sorted(dirty):
            client = self.clients.get(server_name)
            if client is None or "tools" not in dirty[server_name]:
                continue
            capabilities = getattr(client, "server_capabilities", None)
            if capabilities is not None and not capabilities.supports("tools"):
                continue
            tools = await client.list_tools()
            replacement: dict[str, tuple[str, str, dict[str, Any]]] = {}
            collisions: set[str] = set()
            server = next(s for s in self.config.servers if s.name == server_name)
            secrets = _secret_values(server)
            for raw in tools:
                remote = raw.get("name")
                if not isinstance(remote, str):
                    continue
                safe_raw = _redact_value(dict(raw), secrets)
                component = normalize_tool_name(safe_raw["name"])
                public = f"{server.normalized_name}__{component}"
                if not component or not self.config.allows_tool(public) or public in collisions:
                    continue
                if public in replacement:
                    replacement.pop(public, None)
                    collisions.add(public)
                    continue
                replacement[public] = (server_name, remote, safe_raw)
            for public in collisions:
                self._event("mcp_catalog_collision", tool=public)
            self._server_tools[server_name] = replacement

        grouped: dict[str, list[tuple[str, str, dict[str, Any]]]] = {}
        for catalog in self._server_tools.values():
            for public, value in catalog.items():
                grouped.setdefault(public, []).append(value)
        self.remote_tools = {}
        for public, candidates in grouped.items():
            if len(candidates) == 1:
                self.remote_tools[public] = candidates[0]
            else:
                self._event("mcp_catalog_collision", tool=public)
    async def before_model_call(self) -> None:
        if self._dirty:
            await self.refresh()

    def load(self, item_id: str) -> list[str] | None:
        """Validate one catalog id; the caller owns the turn-scoped loaded set."""
        name = item_id.removeprefix("mcp:")
        if name in dict(self.CONTROL_TOOLS) or name in self.remote_tools:
            return [name]
        return None

    def tools(self, loaded: set[str] | tuple[str, ...] = ()) -> tuple[Tool, ...]:
        available = set(dict(self.CONTROL_TOOLS)) | set(self.remote_tools)
        return tuple(self._make_tool(name) for name in sorted(set(loaded) & available))

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
                server_config = next(item for item in self.config.servers if item.name == current[0])
                return mcp_tool_result(
                    await client.call_tool(current[1], kwargs),
                    _secret_values(server_config),
                )

            return Tool(
                name,
                str(raw.get("description") or "MCP tool"),
                call,
                properties,
                tuple(required),
                input_schema=dict(schema),
            )
        return self._control_tool(name)

    def _server(self, server: str) -> MCPClient:
        if server not in self.clients:
            raise ValueError(f"MCP server {server!r} is not initialized")
        return self.clients[server]

    def _control_tool(self, name: str) -> Tool:
        description = dict(self.CONTROL_TOOLS)[name]

        def secrets(server: str) -> tuple[str, ...]:
            config = next(item for item in self.config.servers if item.name == server)
            return _secret_values(config)

        if name == "mcp_resource_list":
            async def handler(server: str, context=None):
                value = await self._server(server).list_resources()
                return compact_json(_redact_value(value, secrets(server)))
            params = {"server": {"type": "string"}}
            required = ("server",)
        elif name == "mcp_resource_templates":
            async def handler(server: str, context=None):
                value = await self._server(server).list_resource_templates()
                return compact_json(_redact_value(value, secrets(server)))
            params = {"server": {"type": "string"}}
            required = ("server",)
        elif name == "mcp_resource_read":
            async def handler(server: str, uri: str, context=None):
                return resource_result(
                    await self._server(server).read_resource(uri), secrets(server)
                )
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
                value = await self._server(server).list_prompts()
                return compact_json(_redact_value(value, secrets(server)))
            params = {"server": {"type": "string"}}
            required = ("server",)
        else:
            async def handler(server: str, name: str, arguments: dict | None = None, context=None):
                result = await self._server(server).get_prompt(name, arguments or None)
                return prompt_result(result, secrets(server))
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
            await asyncio.gather(*(client.close() for client in clients), return_exceptions=True)
