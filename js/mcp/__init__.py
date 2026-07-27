"""Transport-independent Model Context Protocol primitives."""

from .client import MCPClient
from .protocol import JSONRPCError, JSONRPCPeer, PeerClosedError
from .transports import (
    StdioTransport,
    StdioTransportError,
    StreamableHTTPTransport,
    StreamableHTTPTransportError,
)
from .types import LATEST_PROTOCOL_VERSION

__all__ = [
    "JSONRPCError",
    "JSONRPCPeer",
    "LATEST_PROTOCOL_VERSION",
    "MCPClient",
    "PeerClosedError",
    "StdioTransport",
    "StdioTransportError",
    "StreamableHTTPTransport",
    "StreamableHTTPTransportError",
]
