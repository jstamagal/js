"""Base MCP client lifecycle and initialization negotiation."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from .protocol import JSONRPCPeer, RequestHandler, TransportFactory
from .types import (
    LATEST_PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
    ClientCapabilities,
    Implementation,
    InitializeResult,
)


class MCPClientError(RuntimeError):
    pass


class NotInitializedError(MCPClientError):
    pass


class ProtocolVersionError(MCPClientError):
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
    ) -> None:
        self.transport_factory = transport_factory
        self.client_info = Implementation(name=name, version=version, title=title)
        self.capabilities = capabilities or ClientCapabilities()
        self.request_handlers = dict(request_handlers or {})
        self.peer: JSONRPCPeer | None = None
        self.initialize_result: InitializeResult | None = None
        self._initialize_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._closed = False

    @property
    def initialized(self) -> bool:
        return self.initialize_result is not None and self.peer is not None and not self.peer.closed

    @property
    def server_capabilities(self):
        result = self.initialize_result
        return None if result is None else result.capabilities

    async def initialize(self, *, timeout: float | None = None) -> InitializeResult:
        async with self._initialize_lock:
            if self.initialized:
                return self.initialize_result  # type: ignore[return-value]
            if self._closed:
                raise MCPClientError("MCP client is closed")
            peer = await JSONRPCPeer.connect(self.transport_factory)
            self.peer = peer
            for method, handler in self.request_handlers.items():
                peer.add_request_handler(method, handler)
            params = {
                "protocolVersion": LATEST_PROTOCOL_VERSION,
                "capabilities": self.capabilities.to_wire(),
                "clientInfo": self.client_info.to_wire(),
            }
            try:
                raw_result = await peer.request("initialize", params, timeout=timeout)
                result = InitializeResult.from_wire(raw_result)
                if result.protocol_version not in SUPPORTED_PROTOCOL_VERSIONS:
                    raise ProtocolVersionError(
                        f"server selected unsupported MCP protocol version "
                        f"{result.protocol_version!r}"
                    )
                await peer.notify("notifications/initialized")
            except BaseException:
                self.initialize_result = None
                await peer.close()
                self.peer = None
                raise
            self.initialize_result = result
            return result

    async def request(
        self, method: str, params: Mapping[str, Any] | None = None, *, timeout: float | None = None
    ) -> Any:
        return await self._require_peer().request(method, params, timeout=timeout)

    async def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        await self._require_peer().notify(method, params)

    def add_request_handler(self, method: str, handler: RequestHandler) -> None:
        if self.peer is not None:
            self.peer.add_request_handler(method, handler)
        self.request_handlers[method] = handler

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            peer, self.peer = self.peer, None
            self.initialize_result = None
            if peer is not None:
                await peer.close()

    async def __aenter__(self) -> MCPClient:
        await self.initialize()
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self.close()

    def _require_peer(self) -> JSONRPCPeer:
        if not self.initialized or self.peer is None:
            raise NotInitializedError("MCP client has not completed initialization")
        return self.peer


Client = MCPClient
