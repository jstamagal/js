"""Parse and resolve MCP configuration without importing an MCP client.

This module owns only configuration shape and policy.  Connecting to servers is a
separate runtime concern.
"""

from __future__ import annotations

import fnmatch
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

_NAME_PART_RE = re.compile(r"[^a-z0-9_-]+")
_AGENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class MCPConfigError(ValueError):
    """An invalid MCP setting."""


@dataclass(frozen=True)
class MCPServer:
    """One immutable, enabled MCP server definition."""

    name: str
    normalized_name: str
    transport: str
    command: str | None = None
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = MappingProxyType({})
    url: str | None = None
    headers: Mapping[str, str] = MappingProxyType({})


@dataclass(frozen=True)
class MCPPolicy:
    """Resolved allow/deny rules for one agent.  Deny rules always win."""

    server_allow: tuple[str, ...] | None = None
    server_deny: tuple[str, ...] = ()
    tool_allow: tuple[str, ...] | None = None
    tool_deny: tuple[str, ...] = ()

    @staticmethod
    def _allowed(value: str, allow: tuple[str, ...] | None, deny: tuple[str, ...]) -> bool:
        if any(fnmatch.fnmatchcase(value, pattern) for pattern in deny):
            return False
        return allow is None or any(fnmatch.fnmatchcase(value, pattern) for pattern in allow)

    def allows_server(self, server_name: str) -> bool:
        return self._allowed(server_name, self.server_allow, self.server_deny)

    def allows_tool(self, namespaced_tool_name: str) -> bool:
        """Return whether a model-facing ``server__tool`` name is permitted."""
        return self._allowed(namespaced_tool_name, self.tool_allow, self.tool_deny)


@dataclass(frozen=True)
class MCPConfiguration:
    """MCP servers and the deterministic policy for the active agent."""

    servers: tuple[MCPServer, ...]
    policy: MCPPolicy

    def allows_tool(self, namespaced_tool_name: str) -> bool:
        return self.policy.allows_tool(namespaced_tool_name)


def normalize_server_name(name: str) -> str:
    """Normalize a configured server name for its namespaced tool prefix."""
    return _NAME_PART_RE.sub("_", name.strip().lower()).strip("_")


def _pairs_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise MCPConfigError(f"duplicate object key: {key!r}")
        result[key] = value
    return result


def _json_object(raw: str, setting: str) -> dict[str, Any]:
    try:
        value = json.loads(raw, object_pairs_hook=_pairs_object)
    except MCPConfigError:
        raise
    except (json.JSONDecodeError, ValueError) as exc:
        raise MCPConfigError(f"{setting} must be a JSON object") from exc
    if not isinstance(value, dict):
        raise MCPConfigError(f"{setting} must be a JSON object")
    return value


def _string_map(value: Any, field: str) -> Mapping[str, str]:
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not isinstance(item, str) for key, item in value.items()
    ):
        raise MCPConfigError(f"{field} must be an object of string values")
    return MappingProxyType(dict(value))


def _string_list(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise MCPConfigError(f"{field} must be a list of non-empty strings")
    return tuple(value)


def parse_servers(value: Mapping[str, Any]) -> tuple[tuple[MCPServer, bool], ...]:
    """Validate server definitions, retaining enabled state for policy resolution."""
    if not isinstance(value, Mapping):
        raise MCPConfigError("mcp.servers must be a JSON object")
    servers: list[tuple[MCPServer, bool]] = []
    normalized: dict[str, str] = {}
    for name, entry in value.items():
        if not isinstance(name, str) or not name.strip():
            raise MCPConfigError("mcp server names must be non-empty strings")
        normalized_name = normalize_server_name(name)
        if not normalized_name:
            raise MCPConfigError(f"mcp server name {name!r} has no usable characters")
        previous = normalized.get(normalized_name)
        if previous is not None:
            raise MCPConfigError(
                f"mcp server names {previous!r} and {name!r} normalize to the same name"
            )
        normalized[normalized_name] = name
        if not isinstance(entry, dict):
            raise MCPConfigError(f"mcp server {name!r} must be an object")
        unknown = set(entry) - {"enabled", "command", "args", "env", "url", "headers"}
        if unknown:
            raise MCPConfigError(f"mcp server {name!r} has unknown field {sorted(unknown)[0]!r}")
        enabled = entry.get("enabled", True)
        if not isinstance(enabled, bool):
            raise MCPConfigError(f"mcp server {name!r}.enabled must be a boolean")
        has_command = "command" in entry
        has_url = "url" in entry
        if has_command == has_url:
            raise MCPConfigError(f"mcp server {name!r} requires exactly one of command or url")
        if has_command:
            command = entry["command"]
            if not isinstance(command, str) or not command:
                raise MCPConfigError(f"mcp server {name!r}.command must be a non-empty string")
            if "headers" in entry:
                raise MCPConfigError(f"mcp server {name!r}.headers is only valid with url")
            args = _string_list(entry.get("args", []), f"mcp server {name!r}.args")
            env = _string_map(entry.get("env", {}), f"mcp server {name!r}.env")
            server = MCPServer(
                name=name,
                normalized_name=normalized_name,
                transport="stdio",
                command=command,
                args=args,
                env=env,
            )
        else:
            url = entry["url"]
            if not isinstance(url, str) or not url:
                raise MCPConfigError(f"mcp server {name!r}.url must be a non-empty string")
            try:
                parsed = urlsplit(url)
            except ValueError as exc:
                raise MCPConfigError(f"mcp server {name!r}.url is invalid") from exc
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise MCPConfigError(f"mcp server {name!r}.url must use http or https")
            if parsed.username is not None or parsed.password is not None:
                raise MCPConfigError(f"mcp server {name!r}.url must not contain userinfo")
            if "args" in entry or "env" in entry:
                raise MCPConfigError(f"mcp server {name!r} stdio fields require command")
            headers = _string_map(entry.get("headers", {}), f"mcp server {name!r}.headers")
            server = MCPServer(
                name=name,
                normalized_name=normalized_name,
                transport="streamable-http",
                url=url,
                headers=headers,
            )
        servers.append((server, enabled))
    return tuple(servers)


def parse_servers_json(raw: str) -> dict[str, Any]:
    value = _json_object(raw, "mcp.servers")
    parse_servers(value)
    return value


def _policy_patterns(section: Mapping[str, Any], field: str) -> tuple[str, ...] | None:
    if "allow" not in section:
        return None
    return _string_list(section["allow"], field)


def parse_agents(value: Mapping[str, Any]) -> Mapping[str, MCPPolicy]:
    if not isinstance(value, Mapping):
        raise MCPConfigError("mcp.agents must be a JSON object")
    result: dict[str, MCPPolicy] = {}
    for agent_id, entry in value.items():
        if not isinstance(agent_id, str) or _AGENT_ID_RE.fullmatch(agent_id) is None:
            raise MCPConfigError("mcp agent ids may contain only letters, numbers, '_' or '-'")
        if not isinstance(entry, dict):
            raise MCPConfigError(f"mcp agent {agent_id!r} policy must be an object")
        unknown = set(entry) - {"servers", "tools"}
        if unknown:
            raise MCPConfigError(f"mcp agent {agent_id!r} has unknown field {sorted(unknown)[0]!r}")
        sections: dict[str, dict[str, Any]] = {}
        for section in ("servers", "tools"):
            section_value = entry.get(section, {})
            if not isinstance(section_value, dict):
                raise MCPConfigError(f"mcp agent {agent_id!r}.{section} must be an object")
            section_unknown = set(section_value) - {"allow", "deny"}
            if section_unknown:
                raise MCPConfigError(
                    f"mcp agent {agent_id!r}.{section} has unknown field {sorted(section_unknown)[0]!r}"
                )
            sections[section] = section_value
        result[agent_id] = MCPPolicy(
            server_allow=_policy_patterns(
                sections["servers"], f"mcp agent {agent_id!r}.servers.allow"
            ),
            server_deny=_string_list(
                sections["servers"].get("deny", []), f"mcp agent {agent_id!r}.servers.deny"
            ),
            tool_allow=_policy_patterns(
                sections["tools"], f"mcp agent {agent_id!r}.tools.allow"
            ),
            tool_deny=_string_list(
                sections["tools"].get("deny", []), f"mcp agent {agent_id!r}.tools.deny"
            ),
        )
    return MappingProxyType(result)


def parse_agents_json(raw: str) -> dict[str, Any]:
    value = _json_object(raw, "mcp.agents")
    parse_agents(value)
    return value


def resolve(settings: Mapping[str, Any], agent_id: str) -> MCPConfiguration:
    """Resolve enabled servers and active-agent policy from merged settings."""
    mcp = settings.get("mcp", {})
    if not isinstance(mcp, Mapping):
        raise MCPConfigError("mcp settings must be an object")
    parsed_servers = parse_servers(mcp.get("servers", {}))
    policies = parse_agents(mcp.get("agents", {}))
    policy = policies.get(agent_id, MCPPolicy())
    enabled = tuple(
        server
        for server, is_enabled in parsed_servers
        if is_enabled and policy.allows_server(server.name)
    )
    return MCPConfiguration(servers=enabled, policy=policy)
