"""Transport-independent MCP client features and recovery."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Any

from .protocol import JSONRPCPeer, PeerClosedError, RequestHandler, TransportFactory
from .types import (
    LATEST_PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
    CallToolResult,
    ClientCapabilities,
    GetPromptResult,
    Implementation,
    InitializeResult,
    ProgressToken,
    ReadResourceResult,
)

NotificationSink = Callable[[dict[str, Any]], Any | Awaitable[Any]]
Redactor = Callable[[str], str]


class MCPClientError(RuntimeError):
    pass


class NotInitializedError(MCPClientError):
    pass


class ProtocolVersionError(MCPClientError):
    pass


class CapabilityError(MCPClientError):
    pass


class MCPClient:
    def __init__(
        self,
        transport_factory: TransportFactory,
        *,
        name: str = "js",
        version: str = "0.1.0",
        title: str | None = None,
        capabilities: ClientCapabilities | None = None,
        request_handlers: Mapping[str, RequestHandler] | None = None,
        on_tools_changed: NotificationSink | None = None,
        on_resources_changed: NotificationSink | None = None,
        on_resource_updated: NotificationSink | None = None,
        on_prompts_changed: NotificationSink | None = None,
        log_sink: NotificationSink | None = None,
        progress_sink: NotificationSink | None = None,
        secrets: Iterable[str] = (),
        redactor: Redactor | None = None,
        reconnect_attempts: int = 3,
        reconnect_backoff: float = 0.05,
        reconnect_backoff_max: float = 1.0,
    ) -> None:
        self.transport_factory = transport_factory
        self.client_info = Implementation(name=name, version=version, title=title)
        self.capabilities = capabilities or ClientCapabilities()
        self.request_handlers = dict(request_handlers or {})
        self.peer: JSONRPCPeer | None = None
        self.initialize_result: InitializeResult | None = None
        self._negotiated_capabilities = None
        self._initialize_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._closed = False
        self._secrets = tuple(secret for secret in secrets if secret)
        self._custom_redactor = redactor
        self._reconnect_attempts = max(0, reconnect_attempts)
        self._reconnect_backoff = max(0.0, reconnect_backoff)
        self._reconnect_backoff_max = max(0.0, reconnect_backoff_max)
        self._notification_sinks = {
            "notifications/tools/list_changed": on_tools_changed,
            "notifications/resources/list_changed": on_resources_changed,
            "notifications/resources/updated": on_resource_updated,
            "notifications/prompts/list_changed": on_prompts_changed,
            "notifications/message": log_sink,
            "notifications/progress": progress_sink,
        }

    @property
    def initialized(self) -> bool:
        return self.initialize_result is not None and self.peer is not None and not self.peer.closed

    @property
    def server_capabilities(self):
        result = self.initialize_result
        if result is not None:
            return result.capabilities
        return self._negotiated_capabilities

    async def initialize(self, *, timeout: float | None = None) -> InitializeResult:
        async with self._initialize_lock:
            if self.initialized:
                return self.initialize_result  # type: ignore[return-value]
            if self._closed:
                raise MCPClientError("MCP client is closed")
            await self._discard_peer()
            try:
                peer = await JSONRPCPeer.connect(self.transport_factory)
                self.peer = peer
                self._configure_peer(peer)
                raw_result = await peer.request(
                    "initialize",
                    {
                        "protocolVersion": LATEST_PROTOCOL_VERSION,
                        "capabilities": self.capabilities.to_wire(),
                        "clientInfo": self.client_info.to_wire(),
                    },
                    timeout=timeout,
                )
                result = InitializeResult.from_wire(raw_result)
                if result.protocol_version not in SUPPORTED_PROTOCOL_VERSIONS:
                    raise ProtocolVersionError(
                        f"server selected unsupported MCP protocol version "
                        f"{result.protocol_version!r}"
                    )
                await peer.notify("notifications/initialized")
            except asyncio.CancelledError:
                await self._discard_peer()
                raise
            except Exception as exc:
                await self._discard_peer()
                self._raise_redacted(exc)
            self.initialize_result = result
            self._negotiated_capabilities = result.capabilities
            return result

    async def request(
        self, method: str, params: Mapping[str, Any] | None = None, *, timeout: float | None = None
    ) -> Any:
        return await self._require_peer().request(method, params, timeout=timeout)

    async def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        await self._require_peer().notify(method, params)

    async def list_tools(
        self, *, progress_token: ProgressToken | None = None, timeout: float | None = None
    ) -> list[dict[str, Any]]:
        self._require_capability("tools")
        return await self._paginate("tools/list", "tools", progress_token, timeout)

    async def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        progress_token: ProgressToken | None = None,
        timeout: float | None = None,
    ) -> CallToolResult:
        self._require_capability("tools")
        params: dict[str, Any] = {"name": name}
        if arguments is not None:
            params["arguments"] = dict(arguments)
        self._add_progress_token(params, progress_token)
        raw = await self._non_replayable_request("tools/call", params, timeout)
        return CallToolResult.from_wire(raw)

    async def list_resources(
        self, *, progress_token: ProgressToken | None = None, timeout: float | None = None
    ) -> list[dict[str, Any]]:
        self._require_capability("resources")
        return await self._paginate("resources/list", "resources", progress_token, timeout)

    async def list_resource_templates(
        self, *, progress_token: ProgressToken | None = None, timeout: float | None = None
    ) -> list[dict[str, Any]]:
        self._require_capability("resources")
        return await self._paginate(
            "resources/templates/list", "resourceTemplates", progress_token, timeout
        )

    async def read_resource(
        self,
        uri: str,
        *,
        progress_token: ProgressToken | None = None,
        timeout: float | None = None,
    ) -> ReadResourceResult:
        self._require_capability("resources")
        params: dict[str, Any] = {"uri": uri}
        self._add_progress_token(params, progress_token)
        raw = await self._safe_request("resources/read", params, timeout)
        return ReadResourceResult.from_wire(raw)

    async def subscribe_resource(self, uri: str, *, timeout: float | None = None) -> None:
        self._require_resource_subscription()
        await self._non_replayable_request("resources/subscribe", {"uri": uri}, timeout)

    async def unsubscribe_resource(self, uri: str, *, timeout: float | None = None) -> None:
        self._require_resource_subscription()
        await self._non_replayable_request("resources/unsubscribe", {"uri": uri}, timeout)

    async def list_prompts(
        self, *, progress_token: ProgressToken | None = None, timeout: float | None = None
    ) -> list[dict[str, Any]]:
        self._require_capability("prompts")
        return await self._paginate("prompts/list", "prompts", progress_token, timeout)

    async def get_prompt(
        self,
        name: str,
        arguments: Mapping[str, str] | None = None,
        *,
        progress_token: ProgressToken | None = None,
        timeout: float | None = None,
    ) -> GetPromptResult:
        self._require_capability("prompts")
        params: dict[str, Any] = {"name": name}
        if arguments is not None:
            params["arguments"] = dict(arguments)
        self._add_progress_token(params, progress_token)
        raw = await self._safe_request("prompts/get", params, timeout)
        return GetPromptResult.from_wire(raw)

    def add_request_handler(self, method: str, handler: RequestHandler) -> None:
        if self.peer is not None:
            self.peer.add_request_handler(method, handler)
        self.request_handlers[method] = handler

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            await self._discard_peer()
            self._negotiated_capabilities = None

    async def __aenter__(self) -> MCPClient:
        await self.initialize()
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self.close()

    async def _paginate(
        self,
        method: str,
        item_key: str,
        progress_token: ProgressToken | None,
        timeout: float | None,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        seen: set[str] = set()
        while True:
            params: dict[str, Any] = {}
            if cursor is not None:
                params["cursor"] = cursor
            self._add_progress_token(params, progress_token)
            raw = await self._safe_request(method, params or None, timeout)
            if not isinstance(raw, dict):
                raise ValueError(f"{method} result must be an object")
            page = raw.get(item_key)
            if not isinstance(page, list) or not all(isinstance(item, dict) for item in page):
                raise ValueError(f"{method} {item_key} must be a list of objects")
            items.extend(dict(item) for item in page)
            next_cursor = raw.get("nextCursor")
            if next_cursor is None:
                return items
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen:
                raise ValueError(f"{method} returned an invalid nextCursor")
            seen.add(next_cursor)
            cursor = next_cursor

    async def _non_replayable_request(
        self, method: str, params: Mapping[str, Any] | None, timeout: float | None
    ) -> Any:
        try:
            return await self._require_peer().request(method, params, timeout=timeout)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._is_transport_failure(exc):
                await self._discard_peer()
            self._raise_redacted(exc)

    async def _safe_request(
        self, method: str, params: Mapping[str, Any] | None, timeout: float | None
    ) -> Any:
        for attempt in range(self._reconnect_attempts + 1):
            try:
                if not self.initialized:
                    await self.initialize(timeout=timeout)
                return await self._require_peer().request(method, params, timeout=timeout)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                recoverable = self._is_transport_failure(exc) or (
                    not self.initialized
                    and self._negotiated_capabilities is not None
                    and isinstance(exc, MCPClientError)
                )
                if not recoverable or attempt >= self._reconnect_attempts:
                    self._raise_redacted(exc)
                await self._discard_peer()
                delay = min(
                    self._reconnect_backoff * (2**attempt), self._reconnect_backoff_max
                )
                if delay:
                    await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    def _configure_peer(self, peer: JSONRPCPeer) -> None:
        for method, handler in self.request_handlers.items():
            peer.add_request_handler(method, handler)
        for method, sink in self._notification_sinks.items():
            if sink is not None:
                peer.add_notification_handler(method, self._redacting_sink(sink))

    def _redacting_sink(self, sink: NotificationSink) -> NotificationSink:
        async def deliver(params: dict[str, Any]) -> None:
            result = sink(self._redact_value(params))
            if inspect.isawaitable(result):
                await result

        return deliver

    async def _discard_peer(self) -> None:
        peer, self.peer = self.peer, None
        self.initialize_result = None
        if peer is not None:
            await peer.close()

    def _require_peer(self) -> JSONRPCPeer:
        if not self.initialized or self.peer is None:
            raise NotInitializedError("MCP client has not completed initialization")
        return self.peer

    def _require_capability(self, name: str) -> None:
        capabilities = self.server_capabilities
        if capabilities is None:
            raise NotInitializedError("MCP client has not completed initialization")
        if not capabilities.supports(name):
            raise CapabilityError(f"MCP server does not advertise {name!r} capability")

    def _require_resource_subscription(self) -> None:
        self._require_capability("resources")
        resources = self.server_capabilities.raw["resources"]
        if not resources.get("subscribe", False):
            raise CapabilityError("MCP server does not advertise resource subscriptions")

    @staticmethod
    def _add_progress_token(params: dict[str, Any], token: ProgressToken | None) -> None:
        if token is not None:
            params.setdefault("_meta", {})["progressToken"] = token

    @staticmethod
    def _is_transport_failure(exc: BaseException) -> bool:
        return isinstance(exc, (PeerClosedError, ConnectionError, OSError))

    def _redact_text(self, value: str) -> str:
        redacted = value
        for secret in self._secrets:
            redacted = redacted.replace(secret, "[REDACTED]")
        if self._custom_redactor is not None:
            redacted = self._custom_redactor(redacted)
        return redacted

    def _redact_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self._redact_text(value)
        if isinstance(value, dict):
            return {key: self._redact_value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._redact_value(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self._redact_value(item) for item in value)
        return value

    def _raise_redacted(self, exc: Exception) -> None:
        if isinstance(exc, (CapabilityError, NotInitializedError, ProtocolVersionError, ValueError)):
            message = self._redact_text(str(exc))
            raise type(exc)(message) from None
        raise MCPClientError(self._redact_text(str(exc)) or type(exc).__name__) from None


Client = MCPClient
