"""Typed MCP wire models used by the protocol and client layers.

These are deliberately plain standard-library types.  The wire remains dictionaries,
while the small dataclasses give callers stable validated views of initialization data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, NotRequired, TypedDict

LATEST_PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = frozenset({LATEST_PROTOCOL_VERSION})

JSONValue = None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]
JSONRPCId = int | str


class JSONRPCErrorObject(TypedDict):
    code: int
    message: str
    data: NotRequired[JSONValue]


class JSONRPCRequest(TypedDict):
    jsonrpc: str
    id: JSONRPCId
    method: str
    params: NotRequired[dict[str, Any]]


class JSONRPCNotification(TypedDict):
    jsonrpc: str
    method: str
    params: NotRequired[dict[str, Any]]


class JSONRPCResponse(TypedDict):
    jsonrpc: str
    id: JSONRPCId
    result: NotRequired[JSONValue]
    error: NotRequired[JSONRPCErrorObject]


class ImplementationDict(TypedDict):
    name: str
    version: str
    title: NotRequired[str]


class ClientCapabilitiesDict(TypedDict):
    experimental: NotRequired[dict[str, Any]]
    roots: NotRequired[dict[str, bool]]
    sampling: NotRequired[dict[str, Any]]
    elicitation: NotRequired[dict[str, Any]]


class ServerCapabilitiesDict(TypedDict):
    experimental: NotRequired[dict[str, Any]]
    logging: NotRequired[dict[str, Any]]
    prompts: NotRequired[dict[str, bool]]
    resources: NotRequired[dict[str, bool]]
    tools: NotRequired[dict[str, bool]]
    completions: NotRequired[dict[str, Any]]


class TextContentDict(TypedDict):
    type: str
    text: str
    annotations: NotRequired[dict[str, Any]]
    _meta: NotRequired[dict[str, Any]]


class ImageContentDict(TypedDict):
    type: str
    data: str
    mimeType: str
    annotations: NotRequired[dict[str, Any]]
    _meta: NotRequired[dict[str, Any]]


class AudioContentDict(TypedDict):
    type: str
    data: str
    mimeType: str
    annotations: NotRequired[dict[str, Any]]
    _meta: NotRequired[dict[str, Any]]


class ResourceLinkDict(TypedDict):
    type: str
    uri: str
    name: str
    title: NotRequired[str]
    description: NotRequired[str]
    mimeType: NotRequired[str]
    size: NotRequired[int]
    annotations: NotRequired[dict[str, Any]]
    _meta: NotRequired[dict[str, Any]]


class EmbeddedResourceDict(TypedDict):
    type: str
    resource: dict[str, Any]
    annotations: NotRequired[dict[str, Any]]
    _meta: NotRequired[dict[str, Any]]


ContentBlock = (
    TextContentDict | ImageContentDict | AudioContentDict | ResourceLinkDict | EmbeddedResourceDict
)


@dataclass(frozen=True)
class Implementation:
    name: str
    version: str
    title: str | None = None

    def to_wire(self) -> ImplementationDict:
        value: ImplementationDict = {"name": self.name, "version": self.version}
        if self.title is not None:
            value["title"] = self.title
        return value

    @classmethod
    def from_wire(cls, value: object) -> Implementation:
        if not isinstance(value, dict):
            raise ValueError("implementation must be an object")
        name = value.get("name")
        version = value.get("version")
        title = value.get("title")
        if not isinstance(name, str) or not isinstance(version, str):
            raise ValueError("implementation name and version must be strings")
        if title is not None and not isinstance(title, str):
            raise ValueError("implementation title must be a string")
        return cls(name=name, version=version, title=title)


@dataclass(frozen=True)
class ClientCapabilities:
    """Capabilities this client actually implements.

    Empty capabilities are intentional for the base client.  Future integration can
    opt into request handlers and advertise the matching capability at construction.
    """

    roots_list_changed: bool | None = None
    sampling: bool = False
    elicitation: bool = False
    experimental: dict[str, Any] = field(default_factory=dict)

    def to_wire(self) -> ClientCapabilitiesDict:
        value: ClientCapabilitiesDict = {}
        if self.experimental:
            value["experimental"] = dict(self.experimental)
        if self.roots_list_changed is not None:
            value["roots"] = {"listChanged": self.roots_list_changed}
        if self.sampling:
            value["sampling"] = {}
        if self.elicitation:
            value["elicitation"] = {}
        return value


@dataclass(frozen=True)
class ServerCapabilities:
    raw: dict[str, Any]

    @classmethod
    def from_wire(cls, value: object) -> ServerCapabilities:
        if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
            raise ValueError("server capabilities must be an object")
        known = {"experimental", "logging", "prompts", "resources", "tools", "completions"}
        for name, capability in value.items():
            if name in known and not isinstance(capability, dict):
                raise ValueError(f"server capability {name!r} must be an object")
        return cls(raw=dict(value))

    def supports(self, name: str) -> bool:
        return name in self.raw


@dataclass(frozen=True)
class InitializeResult:
    protocol_version: str
    capabilities: ServerCapabilities
    server_info: Implementation
    instructions: str | None = None

    @classmethod
    def from_wire(cls, value: object) -> InitializeResult:
        if not isinstance(value, dict):
            raise ValueError("initialize result must be an object")
        protocol_version = value.get("protocolVersion")
        if not isinstance(protocol_version, str):
            raise ValueError("initialize result has no protocolVersion")
        instructions = value.get("instructions")
        if instructions is not None and not isinstance(instructions, str):
            raise ValueError("initialize instructions must be a string")
        return cls(
            protocol_version=protocol_version,
            capabilities=ServerCapabilities.from_wire(value.get("capabilities")),
            server_info=Implementation.from_wire(value.get("serverInfo")),
            instructions=instructions,
        )
