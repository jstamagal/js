"""Lazy MCP host integration for one js turn/session."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import urllib.parse
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


def _query_secret_values(url: str) -> set[str]:
    """Return wire and recursively decoded URL query values."""
    values: set[str] = set()
    query = urllib.parse.urlsplit(url).query
    for field in query.split("&"):
        _key, separator, raw = field.partition("=")
        if not separator or not raw:
            continue
        pending = [raw]
        while pending:
            value = pending.pop()
            if not value or value in values:
                continue
            values.add(value)
            pending.extend((urllib.parse.unquote(value), urllib.parse.unquote_plus(value)))
    return values


def _secret_values(server: MCPServer) -> tuple[str, ...]:
    values = [value for value in (*server.env.values(), *server.headers.values()) if value]
    if server.url:
        # Query credentials may be echoed in their configured wire form, decoded
        # by a server framework, or re-encoded by server-controlled output.
        values.extend(_query_secret_values(server.url))
    # stdio arguments commonly carry tokens; flags themselves are not
    # secrets and very short args would over-redact ordinary output.
    following_flag = False
    for arg in server.args:
        if not arg:
            following_flag = False
            continue
        if arg.startswith("-"):
            # Inline credential form: --token=SECRET. The value side is an
            # explicitly configured credential position; no length floor.
            _flag, separator, inline = arg.partition("=")
            if separator and inline:
                values.append(inline)
            following_flag = not separator
        else:
            # A value right after a bare flag (--token abc) is a credential
            # position at any length; standalone positionals keep a floor so
            # subcommands like "serve" are not scrubbed from ordinary text.
            if following_flag or len(arg) >= 6:
                values.append(arg)
            following_flag = False
    secrets = set(values)
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


def _decode_percent_layer(value: str, spans: list[tuple[int, int]], *, plus: bool) -> tuple[str, list[tuple[int, int]]]:
    """Decode one URL-escape layer while retaining each character's source span."""
    output: list[str] = []
    mapped: list[tuple[int, int]] = []
    index = 0
    while index < len(value):
        if value[index] == "%" and index + 2 < len(value) and all(
            char in "0123456789abcdefABCDEF" for char in value[index + 1:index + 3]
        ):
            start = index
            encoded = bytearray()
            while index + 2 < len(value) and value[index] == "%" and all(
                char in "0123456789abcdefABCDEF" for char in value[index + 1:index + 3]
            ):
                # int(..., 16) deliberately case-folds wire escape hex.
                encoded.append(int(value[index + 1:index + 3], 16))
                index += 3
            decoded = encoded.decode("utf-8", errors="replace")
            source = (spans[start][0], spans[index - 1][1])
            output.extend(decoded)
            mapped.extend(source for _ in decoded)
            continue
        output.append(" " if plus and value[index] == "+" else value[index])
        mapped.append(spans[index])
        index += 1
    return "".join(output), mapped


def _normalized_candidates(value: str) -> tuple[tuple[str, list[tuple[int, int]]], ...]:
    """Return percent-decoded candidates mapped back to spans in ``value``."""
    initial = (value, [(index, index + 1) for index in range(len(value))])
    pending = [initial]
    candidates: dict[str, list[tuple[int, int]]] = {}
    while pending:
        candidate, spans = pending.pop()
        if candidate in candidates:
            continue
        candidates[candidate] = spans
        for plus in (False, True):
            decoded = _decode_percent_layer(candidate, spans, plus=plus)
            if decoded[0] not in candidates:
                pending.append(decoded)
    return tuple(candidates.items())


def _redact_text(value: str, secrets: tuple[str, ...]) -> str:
    normalized_secrets = {
        candidate
        for secret in secrets
        for candidate, _spans in _normalized_candidates(secret)
        if candidate
    }
    redactions: list[tuple[int, int]] = []
    for candidate, spans in _normalized_candidates(value):
        for secret in normalized_secrets:
            start = 0
            while (match := candidate.find(secret, start)) >= 0:
                covered = spans[match:match + len(secret)]
                redactions.append((min(span[0] for span in covered), max(span[1] for span in covered)))
                start = match + len(secret)
    merged: list[tuple[int, int]] = []
    for start, end in sorted(set(redactions)):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    for start, end in reversed(merged):
        value = value[:start] + "[REDACTED]" + value[end:]
    return value


MAX_PUBLIC_TOOL_NAME = 64
DEFAULT_REQUEST_TIMEOUT = 10.0


def _bounded_public_name(server: str, component: str, limit: int = MAX_PUBLIC_TOOL_NAME) -> str:
    """server__tool, trimmed to the provider's function-name limit.

    Providers commonly reject names over 64 characters, which would fail the
    whole request rather than hide one tool. Keep the server prefix readable
    and shorten the tool component, ending with a short hash so two long
    remote names cannot collapse onto the same public name.
    """
    public = f"{server}__{component}"
    if len(public) <= limit:
        return public
    digest = hashlib.sha256(public.encode("utf-8")).hexdigest()[:6]
    prefix = server[: max(1, min(len(server), limit // 3))]
    room = limit - len(prefix) - len(digest) - 3  # two underscores plus one
    return f"{prefix}__{component[: max(1, room)]}_{digest}"


def _collision_public_name(
    server: str,
    component: str,
    remote: str,
    occurrence: int,
    *,
    scope: str = "",
) -> str:
    """Return a stable bounded alias for one normalization-colliding remote name."""
    digest = hashlib.sha256(
        f"{scope}\0{remote}\0{occurrence}".encode()
    ).hexdigest()[:6]
    return _bounded_public_name(server, f"{component}_{digest}")


def _redact_value(value: Any, secrets: tuple[str, ...]) -> Any:
    """Recursively scrub configured credentials from server-controlled data."""
    if isinstance(value, str):
        return _redact_text(value, secrets)
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


def _safe_media_data(data: Any, secrets: tuple[str, ...]) -> bool:
    """Accept only strict base64 whose decoded bytes contain no credential."""
    try:
        decoded = base64.b64decode(str(data), validate=True)
    except (ValueError, TypeError):
        return False
    if any(secret.encode() in decoded for secret in secrets):
        return False
    try:
        text = decoded.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return _redact_text(text, secrets) == text


def _safe_content_block(raw: Any, secrets: tuple[str, ...]) -> dict[str, Any]:
    """Redact text and replace unsafe media before it can reach model conversion."""
    item = _redact_value(dict(raw), secrets)
    kind = item.get("type")
    if kind in {"image", "audio"} and not _safe_media_data(item.get("data", ""), secrets):
        return {"type": "text", "text": f"[{kind} content suppressed]"}
    if kind == "resource":
        resource = item.get("resource")
        if (
            isinstance(resource, dict)
            and "blob" in resource
            and not _safe_media_data(resource["blob"], secrets)
        ):
            return {"type": "text", "text": "[embedded resource content suppressed]"}
    return item


def mcp_tool_result(result: Any, secrets: tuple[str, ...] = ()) -> ToolResult:
    blocks = [_safe_content_block(item, secrets) for item in result.content]
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
            if not _safe_media_data(item["blob"], secrets):
                blocks.append({"type": "text", "text": "[resource content suppressed]"})
                continue
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
        item = _safe_content_block(content, secrets)
        if item.get("type") == "text":
            blocks.append({"type": "text", "text": f"[{role}]\n{item.get('text', '')}"})
        else:
            blocks.append({"type": "text", "text": f"[{role}]"})
            blocks.append(item)
    return ToolResult(blocks=blocks)


_MCP_CONTROL_SETUP = (
    " Before using this control, run tool_discovery with kind=\"mcp\" (and "
    "optionally source) to connect servers, then pass the exact server name from "
    "a mcp:server:* status result."
)
_MCP_RESOURCE_URI = (
    " First call mcp_resource_list or mcp_resource_templates and copy an exact "
    "returned URI."
)
_MCP_PROMPT_NAME = (
    " First call mcp_prompt_list and copy an exact returned prompt name."
)


class MCPHost:
    """Own clients and lazy remote metadata for one js session."""

    CONTROL_TOOLS = (
        ("mcp_resource_list", "List resources from an MCP server." + _MCP_CONTROL_SETUP),
        ("mcp_resource_templates", "List resource templates from an MCP server." + _MCP_CONTROL_SETUP),
        ("mcp_resource_read", "Read an MCP resource by URI." + _MCP_CONTROL_SETUP + _MCP_RESOURCE_URI),
        ("mcp_resource_subscribe", "Subscribe to updates for an MCP resource." + _MCP_CONTROL_SETUP + _MCP_RESOURCE_URI),
        ("mcp_resource_unsubscribe", "Unsubscribe from updates for an MCP resource." + _MCP_CONTROL_SETUP + _MCP_RESOURCE_URI),
        ("mcp_prompt_list", "List prompts from an MCP server." + _MCP_CONTROL_SETUP),
        ("mcp_prompt_get", "Get an MCP prompt and its messages." + _MCP_CONTROL_SETUP + _MCP_PROMPT_NAME),
    )

    def __init__(
        self,
        config: MCPConfiguration,
        *,
        client_factory: Callable[..., MCPClient] = MCPClient,
        telemetry: Any = None,
        event_sink: Callable[..., Any] | None = None,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
    ) -> None:
        if request_timeout <= 0:
            raise ValueError("MCP request timeout must be positive")
        self.config = config
        self._client_factory = client_factory
        self.telemetry = telemetry
        self.event_sink = event_sink
        self.request_timeout = float(request_timeout)
        self.clients: dict[str, MCPClient] = {}
        self.remote_tools: dict[str, tuple[str, str, dict[str, Any]]] = {}
        self._server_tools: dict[str, dict[str, tuple[str, str, dict[str, Any]]]] = {}
        self._server_errors: dict[str, str] = {}
        self._server_collisions: dict[str, tuple[CatalogEntry, ...]] = {}
        self._cross_server_collisions: tuple[CatalogEntry, ...] = ()
        self._dirty: dict[str, set[str]] = {}
        self._reserved_public_names: set[str] = set()
        self._reported_public_collisions: set[str] = set()
        self._closed = False

    def reserve_public_names(self, names: set[str]) -> None:
        """Withhold MCP names already claimed by the turn's native surface."""
        self._reserved_public_names.update(str(name).casefold() for name in names)
        for public in tuple(self.remote_tools):
            if self._public_name_reserved(public):
                self.remote_tools.pop(public, None)

    def _public_name_reserved(self, public: str) -> bool:
        if public.casefold() not in self._reserved_public_names:
            return False
        if public not in self._reported_public_collisions:
            self._reported_public_collisions.add(public)
            self._event("mcp_catalog_collision", tool=public)
        return True

    def initial_catalog(self) -> tuple[CatalogEntry, ...]:
        """Only generic controls are visible before an MCP-scoped discovery."""
        return tuple(
            CatalogEntry(f"mcp:{name}", name, description, "mcp", "mcp")
            for name, description in self.CONTROL_TOOLS
            if self.config.allows_tool(name) and not self._public_name_reserved(name)
        )

    def is_server_source(self, source: str) -> bool:
        """Return whether source exactly names one configured MCP server."""
        scope = str(source).strip().casefold()
        return any(
            scope in {server.name.casefold(), server.normalized_name.casefold()}
            for server in self.config.servers
        )

    async def discover(self, query: str = "", source: str = "") -> tuple[CatalogEntry, ...]:
        terms = [term for term in str(query).casefold().split() if term != "mcp"]
        servers = [
            server for server in self.config.servers
            if self.config.policy.allows_server(server.name)
        ]
        source_scope = str(source).strip().casefold()
        if source_scope and source_scope != "mcp":
            servers = [
                server for server in servers
                if source_scope in {server.name.casefold(), server.normalized_name.casefold()}
            ]
        elif terms:
            matching = [
                server for server in servers
                if all(term in f"{server.name} {server.normalized_name} {server.transport}".casefold() for term in terms)
            ]
            # A query for remote metadata cannot be resolved before connecting.
            # Narrow only when configured server metadata identifies candidates.
            if matching:
                servers = matching
        for server in servers:
            try:
                await self._ensure_server(server)
                self._server_errors.pop(server.name, None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._record_server_error(server, exc)
        await self.refresh()
        entries = list(self.initial_catalog())
        for server_name, message in self._server_errors.items():
            server = next(
                (item for item in self.config.servers if item.name == server_name),
                None,
            )
            if server is None or not self.config.policy.allows_server(server.name):
                continue
            entries.append(CatalogEntry(
                f"mcp:server-error:{server.normalized_name}",
                server.name,
                f"MCP server error: {message}"[:240],
                "mcp",
                server.name,
                loadable=False,
            ))
        for collision_entries in self._server_collisions.values():
            entries.extend(collision_entries)
        entries.extend(self._cross_server_collisions)
        for server_name, client in self.clients.items():
            if not getattr(client, "initialized", False):
                continue
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
                loadable=False,
            ))
        for public, (server_name, _remote, raw) in self.remote_tools.items():
            description = str(raw.get("description") or raw.get("title") or "MCP tool")[:240]
            entries.append(CatalogEntry(f"mcp:{public}", public, description, "mcp", server_name))
        result = []
        for entry in sorted(entries, key=lambda item: item.id):
            haystack = f"{entry.id} {entry.name} {entry.description} {entry.source}".casefold()
            if source and entry.source.casefold() != source.casefold():
                server = next(
                    (item for item in self.config.servers if item.name == entry.source),
                    None,
                )
                if server is None or server.normalized_name.casefold() != source.casefold():
                    continue
            if terms and not all(term in haystack for term in terms):
                continue
            result.append(entry)
        return tuple(result)

    def _record_server_error(self, server: MCPServer, exc: Exception) -> None:
        message = str(exc).strip() or type(exc).__name__
        message = _redact_text(message, _secret_values(server))
        self._server_errors[server.name] = message
        self._event("mcp_server_error", server=server.name, error=message)

    async def _ensure_server(self, server: MCPServer) -> MCPClient:
        client = self.clients.get(server.name)
        if client is not None:
            if not client.initialized:
                await client.initialize(timeout=self.request_timeout)
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
            # The client's own replacement is literal; give it the host's
            # encoding-aware scrubber so a server echoing a%3Ab for a:b in a
            # JSON-RPC error cannot carry the credential out through an
            # exception message.
            redactor=(lambda text: _redact_text(text, secrets)),
        )
        self.clients[server.name] = client
        await client.initialize(timeout=self.request_timeout)
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
            server = next(s for s in self.config.servers if s.name == server_name)
            try:
                tools = await client.list_tools(timeout=self.request_timeout)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._record_server_error(server, exc)
                self._dirty.setdefault(server_name, set()).add("tools")
                continue
            replacement: dict[str, tuple[str, str, dict[str, Any]]] = {}
            grouped_tools: dict[
                str, list[tuple[str, dict[str, Any], str]]
            ] = {}
            secrets = _secret_values(server)
            for raw in tools:
                remote = raw.get("name")
                if not isinstance(remote, str):
                    continue
                safe_raw = _redact_value(dict(raw), secrets)
                component = normalize_tool_name(safe_raw["name"])
                public = _bounded_public_name(server.normalized_name, component)
                if not component:
                    continue
                grouped_tools.setdefault(public, []).append((remote, safe_raw, component))
            collision_entries: list[CatalogEntry] = []
            for public, candidates in grouped_tools.items():
                if len(candidates) == 1:
                    remote, safe_raw, _component = candidates[0]
                    if (
                        self.config.allows_tool(public)
                        and not self._public_name_reserved(public)
                    ):
                        replacement[public] = (server_name, remote, safe_raw)
                    continue
                self._event(
                    "mcp_catalog_collision",
                    tool=public,
                    server=server_name,
                    remote_names=[remote for remote, _safe, _component in candidates],
                )
                exposed: list[tuple[str, str]] = []
                occurrences: dict[str, int] = {}
                for remote, safe_raw, component in candidates:
                    occurrence = occurrences.get(remote, 0)
                    occurrences[remote] = occurrence + 1
                    disambiguated = _collision_public_name(
                        server.normalized_name, component, remote, occurrence
                    )
                    if (
                        not self.config.allows_tool(disambiguated)
                        or self._public_name_reserved(disambiguated)
                    ):
                        continue
                    replacement[disambiguated] = (server_name, remote, safe_raw)
                    exposed.append((str(safe_raw["name"]), disambiguated))
                if exposed:
                    digest = hashlib.sha256(
                        f"{server.name}\0{public}".encode()
                    ).hexdigest()[:6]
                    names = ", ".join(name for name, _alias in exposed)
                    aliases = ", ".join(alias for _name, alias in exposed)
                    collision_entries.append(CatalogEntry(
                        f"mcp:collision:{server.normalized_name}:{digest}",
                        public,
                        (
                            f"MCP tool names {names} normalize to the same public name; "
                            f"exposed with disambiguated names: {aliases}"
                        )[:240],
                        "mcp",
                        server.name,
                        loadable=False,
                    ))
            self._server_errors.pop(server_name, None)
            self._server_collisions[server_name] = tuple(collision_entries)
            self._server_tools[server_name] = replacement

        grouped: dict[str, list[tuple[str, str, dict[str, Any]]]] = {}
        for catalog in self._server_tools.values():
            for public, value in catalog.items():
                grouped.setdefault(public, []).append(value)
        self.remote_tools = {}
        claimed_public_names = set(grouped)
        cross_server_collisions: list[CatalogEntry] = []
        for public, candidates in grouped.items():
            if self._public_name_reserved(public):
                continue
            if len(candidates) == 1:
                self.remote_tools[public] = candidates[0]
            else:
                self._event(
                    "mcp_catalog_collision",
                    tool=public,
                    servers=[server for server, _remote, _raw in candidates],
                )
                exposed: list[tuple[str, str, str]] = []
                occurrences: dict[tuple[str, str], int] = {}
                for server_name, remote, raw in candidates:
                    server = next(
                        item for item in self.config.servers
                        if item.name == server_name
                    )
                    component = normalize_tool_name(str(raw.get("name") or remote))
                    occurrence_key = (server_name, remote)
                    occurrence = occurrences.get(occurrence_key, 0)
                    occurrences[occurrence_key] = occurrence + 1
                    while True:
                        alias = _collision_public_name(
                            server.normalized_name,
                            component,
                            remote,
                            occurrence,
                            scope=server.name,
                        )
                        if (
                            alias not in claimed_public_names
                            and alias not in self.remote_tools
                        ):
                            break
                        occurrence += 1
                    if (
                        not self.config.allows_tool(alias)
                        or self._public_name_reserved(alias)
                    ):
                        continue
                    self.remote_tools[alias] = (server_name, remote, raw)
                    exposed.append((server_name, remote, alias))
                digest = hashlib.sha256(
                    (public + "\0" + "\0".join(
                        f"{server}/{remote}" for server, remote, _raw in candidates
                    )).encode()
                ).hexdigest()[:6]
                mappings = ", ".join(
                    f"{server}/{remote} as mcp:{alias}"
                    for server, remote, alias in exposed
                )
                if not mappings:
                    mappings = "no disambiguated ID is allowed by current tool policy"
                cross_server_collisions.append(CatalogEntry(
                    f"mcp:collision:cross:{digest}",
                    public,
                    (
                        f"MCP tools from multiple servers collide as {public}; "
                        f"use disambiguated IDs {mappings}."
                    ),
                    "mcp",
                    "mcp",
                    loadable=False,
                ))
        self._cross_server_collisions = tuple(cross_server_collisions)
    async def before_model_call(self) -> None:
        if self._dirty:
            await self.refresh()

    def load(self, item_id: str) -> list[str] | None:
        """Validate one catalog id; the caller owns the turn-scoped loaded set."""
        name = item_id.removeprefix("mcp:")
        if not self.config.allows_tool(name) or self._public_name_reserved(name):
            return None
        if name in dict(self.CONTROL_TOOLS) or name in self.remote_tools:
            return [name]
        return None

    def tools(self, loaded: set[str] | tuple[str, ...] = ()) -> tuple[Tool, ...]:
        available = {
            name for name in set(dict(self.CONTROL_TOOLS)) | set(self.remote_tools)
            if self.config.allows_tool(name) and not self._public_name_reserved(name)
        }
        return tuple(self._make_tool(name) for name in sorted(set(loaded) & available))

    def _make_tool(self, name: str) -> Tool:
        remote = self.remote_tools.get(name)
        if remote is not None:
            server, remote_name, raw = remote
            schema = raw.get("inputSchema") if isinstance(raw.get("inputSchema"), dict) else {}
            properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
            required = schema.get("required") if isinstance(schema.get("required"), list) else []

            declares_context = "context" in properties

            async def call(context=None, **kwargs: Any) -> ToolResult:
                if declares_context and context is not None:
                    # The schema's own context property must reach the server;
                    # it binds to this wrapper parameter, not kwargs.
                    kwargs["context"] = context
                current = self.remote_tools.get(name)
                if current is None or not self.config.allows_tool(name):
                    return ToolResult.text(f"ERROR: MCP tool {name!r} is no longer available", is_error=True)
                client = self.clients[current[0]]
                server_config = next(item for item in self.config.servers if item.name == current[0])
                return mcp_tool_result(
                    await client.call_tool(
                        current[1], kwargs, timeout=self.request_timeout
                    ),
                    _secret_values(server_config),
                )

            valid_input_schema = (
                isinstance(raw.get("inputSchema"), dict)
                and schema.get("type") == "object"
                and isinstance(schema.get("properties"), dict)
            )
            return Tool(
                name,
                str(raw.get("description") or "MCP tool"),
                call,
                properties,
                tuple(required),
                input_schema=dict(schema) if valid_input_schema else None,
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
                value = await self._server(server).list_resources(
                    timeout=self.request_timeout
                )
                return compact_json(_redact_value(value, secrets(server)))
            params = {"server": {"type": "string"}}
            required = ("server",)
        elif name == "mcp_resource_templates":
            async def handler(server: str, context=None):
                value = await self._server(server).list_resource_templates(
                    timeout=self.request_timeout
                )
                return compact_json(_redact_value(value, secrets(server)))
            params = {"server": {"type": "string"}}
            required = ("server",)
        elif name == "mcp_resource_read":
            async def handler(server: str, uri: str, context=None):
                return resource_result(
                    await self._server(server).read_resource(
                        uri, timeout=self.request_timeout
                    ),
                    secrets(server),
                )
            params = {"server": {"type": "string"}, "uri": {"type": "string"}}
            required = ("server", "uri")
        elif name in {"mcp_resource_subscribe", "mcp_resource_unsubscribe"}:
            async def handler(server: str, uri: str, context=None):
                client = self._server(server)
                method = client.subscribe_resource if name.endswith("subscribe") and not name.endswith("unsubscribe") else client.unsubscribe_resource
                await method(uri, timeout=self.request_timeout)
                return compact_json({"server": server, "uri": uri, "subscribed": method == client.subscribe_resource})
            params = {"server": {"type": "string"}, "uri": {"type": "string"}}
            required = ("server", "uri")
        elif name == "mcp_prompt_list":
            async def handler(server: str, context=None):
                value = await self._server(server).list_prompts(
                    timeout=self.request_timeout
                )
                return compact_json(_redact_value(value, secrets(server)))
            params = {"server": {"type": "string"}}
            required = ("server",)
        else:
            async def handler(server: str, name: str, arguments: dict | None = None, context=None):
                result = await self._server(server).get_prompt(
                    name, arguments or None, timeout=self.request_timeout
                )
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
