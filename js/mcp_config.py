"""Parse and resolve MCP configuration without importing an MCP client.

This module owns only configuration shape and policy.  Connecting to servers is a
separate runtime concern.
"""

from __future__ import annotations

from . import messages as msgs

import fnmatch
import json
import math
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

    def allows_server(self, *names: str) -> bool:
        """Whether a server is permitted, given every name it goes by (the
        configured name and its normalized form). A deny on ANY name wins;
        otherwise an allow list must match at least one name."""
        if any(fnmatch.fnmatchcase(name, pattern) for name in names for pattern in self.server_deny):
            return False
        allow = self.server_allow
        return allow is None or any(fnmatch.fnmatchcase(name, pattern) for name in names for pattern in allow)

    def allows_tool(self, namespaced_tool_name: str) -> bool:
        """Return whether a model-facing ``server__tool`` name is permitted."""
        return self._allowed(namespaced_tool_name, self.tool_allow, self.tool_deny)


@dataclass(frozen=True)
class MCPConfiguration:
    """MCP servers and the deterministic policy for the active agent."""

    servers: tuple[MCPServer, ...]
    policy: MCPPolicy
    request_timeout_s: float | None = None  # MCP_TIMEOUT; None = its js/jsrc value

    def allows_tool(self, namespaced_tool_name: str) -> bool:
        return self.policy.allows_tool(namespaced_tool_name)


def normalize_server_name(name: str) -> str:
    """Normalize a configured server name for its namespaced tool prefix."""
    return _NAME_PART_RE.sub("_", name.strip().lower()).strip("_")


def _pairs_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise MCPConfigError(msgs.MCP_CONFIG_DUPLICATE_OBJECT_KEY.format(key=key))
        result[key] = value
    return result


def _json_object(raw: str, setting: str) -> dict[str, Any]:
    try:
        value = json.loads(raw, object_pairs_hook=_pairs_object)
    except MCPConfigError:
        raise
    except (json.JSONDecodeError, ValueError) as exc:
        raise MCPConfigError(msgs.MCP_CONFIG_MUST_BE_A_JSON_OBJECT.format(setting=setting)) from exc
    if not isinstance(value, dict):
        raise MCPConfigError(msgs.MCP_CONFIG_MUST_BE_A_JSON_OBJECT.format(setting=setting))
    return value


def _string_map(value: Any, field: str) -> Mapping[str, str]:
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not isinstance(item, str) for key, item in value.items()
    ):
        raise MCPConfigError(msgs.MCP_CONFIG_MUST_BE_AN_OBJECT_OF_STRING_VALUES.format(field=field))
    return MappingProxyType(dict(value))


def _string_list(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise MCPConfigError(msgs.MCP_CONFIG_MUST_BE_A_LIST_OF_NON_EMPTY.format(field=field))
    return tuple(value)


def parse_servers(value: Mapping[str, Any]) -> tuple[tuple[MCPServer, bool], ...]:
    """Validate server definitions, retaining enabled state for policy resolution."""
    if not isinstance(value, Mapping):
        raise MCPConfigError(msgs.MCP_CONFIG_MCP_SERVERS_MUST_BE_A_JSON_OBJECT.format())
    servers: list[tuple[MCPServer, bool]] = []
    normalized: dict[str, str] = {}
    for name, entry in value.items():
        if not isinstance(name, str) or not name.strip():
            raise MCPConfigError(msgs.MCP_CONFIG_MCP_SERVER_NAMES_MUST_BE_NON_EMPTY.format())
        normalized_name = normalize_server_name(name)
        if not normalized_name:
            raise MCPConfigError(msgs.MCP_CONFIG_MCP_SERVER_NAME_HAS_NO_USABLE_CHARACTERS.format(name=name))
        previous = normalized.get(normalized_name)
        if previous is not None:
            raise MCPConfigError(
                msgs.MCP_CONFIG_MCP_SERVER_NAMES_AND_NORMALIZE_TO_THE.format(previous=previous, name=name)
            )
        normalized[normalized_name] = name
        if not isinstance(entry, dict):
            raise MCPConfigError(msgs.MCP_CONFIG_MCP_SERVER_MUST_BE_AN_OBJECT.format(name=name))
        unknown = set(entry) - {"enabled", "command", "args", "env", "url", "headers"}
        if unknown:
            raise MCPConfigError(msgs.MCP_CONFIG_MCP_SERVER_HAS_UNKNOWN_FIELD.format(name=name, value=sorted(unknown)[0]))
        enabled = entry.get("enabled", True)
        if not isinstance(enabled, bool):
            raise MCPConfigError(msgs.MCP_CONFIG_MCP_SERVER_ENABLED_MUST_BE_A_BOOLEAN.format(name=name))
        has_command = "command" in entry
        has_url = "url" in entry
        if has_command == has_url:
            raise MCPConfigError(msgs.MCP_CONFIG_MCP_SERVER_REQUIRES_EXACTLY_ONE_OF_COMMAND.format(name=name))
        if has_command:
            command = entry["command"]
            if not isinstance(command, str) or not command:
                raise MCPConfigError(msgs.MCP_CONFIG_MCP_SERVER_COMMAND_MUST_BE_A_NON.format(name=name))
            if "headers" in entry:
                raise MCPConfigError(msgs.MCP_CONFIG_MCP_SERVER_HEADERS_IS_ONLY_VALID_WITH.format(name=name))
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
                raise MCPConfigError(msgs.MCP_CONFIG_MCP_SERVER_URL_MUST_BE_A_NON.format(name=name))
            try:
                parsed = urlsplit(url)
            except ValueError as exc:
                raise MCPConfigError(msgs.MCP_CONFIG_MCP_SERVER_URL_IS_INVALID.format(name=name)) from exc
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise MCPConfigError(msgs.MCP_CONFIG_MCP_SERVER_URL_MUST_USE_HTTP_OR.format(name=name))
            if parsed.username is not None or parsed.password is not None:
                raise MCPConfigError(msgs.MCP_CONFIG_MCP_SERVER_URL_MUST_NOT_CONTAIN_USERINFO.format(name=name))
            if "args" in entry or "env" in entry:
                raise MCPConfigError(msgs.MCP_CONFIG_MCP_SERVER_STDIO_FIELDS_REQUIRE_COMMAND.format(name=name))
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
    value = _json_object(raw, "MCP_SERVERS")
    parse_servers(value)
    return value


def _policy_patterns(section: Mapping[str, Any], field: str) -> tuple[str, ...] | None:
    if "allow" not in section:
        return None
    return _string_list(section["allow"], field)


def parse_agents(value: Mapping[str, Any]) -> Mapping[str, MCPPolicy]:
    if not isinstance(value, Mapping):
        raise MCPConfigError(msgs.MCP_CONFIG_MCP_AGENT_POLICY_MUST_BE_A_JSON.format())
    result: dict[str, MCPPolicy] = {}
    for agent_id, entry in value.items():
        if not isinstance(agent_id, str) or _AGENT_ID_RE.fullmatch(agent_id) is None:
            raise MCPConfigError(msgs.MCP_CONFIG_MCP_AGENT_IDS_MAY_CONTAIN_ONLY_LETTERS.format())
        if not isinstance(entry, dict):
            raise MCPConfigError(msgs.MCP_CONFIG_MCP_AGENT_POLICY_MUST_BE_AN_OBJECT.format(agent_id=agent_id))
        unknown = set(entry) - {"servers", "tools"}
        if unknown:
            raise MCPConfigError(msgs.MCP_CONFIG_MCP_AGENT_HAS_UNKNOWN_FIELD.format(agent_id=agent_id, value=sorted(unknown)[0]))
        sections: dict[str, dict[str, Any]] = {}
        for section in ("servers", "tools"):
            section_value = entry.get(section, {})
            if not isinstance(section_value, dict):
                raise MCPConfigError(msgs.MCP_CONFIG_MCP_AGENT_MUST_BE_AN_OBJECT.format(agent_id=agent_id, section=section))
            section_unknown = set(section_value) - {"allow", "deny"}
            if section_unknown:
                raise MCPConfigError(
                    msgs.MCP_CONFIG_MCP_AGENT_HAS_UNKNOWN_FIELD_226.format(agent_id=agent_id, section=section, value=sorted(section_unknown)[0])
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
    value = _json_object(raw, "MCP_AGENT_POLICY")
    parse_agents(value)
    return value


def resolve(settings: Mapping[str, Any], agent_id: str) -> MCPConfiguration:
    """Resolve enabled servers and active-agent policy from merged settings."""
    parsed_servers = parse_servers(settings.get("MCP_SERVERS") or {})
    policies = parse_agents(settings.get("MCP_AGENT_POLICY") or {})
    policy = policies.get(agent_id, MCPPolicy())
    enabled = tuple(
        server
        for server, is_enabled in parsed_servers
        if is_enabled and policy.allows_server(server.name, server.normalized_name)
    )
    timeout = settings.get("MCP_TIMEOUT")
    return MCPConfiguration(
        servers=enabled,
        policy=policy,
        request_timeout_s=_request_timeout(timeout),
    )


def _request_timeout(value: Any) -> float | None:
    """``MCP_TIMEOUT`` as a positive finite float; None when unset.
    Anything else is a config error now, not a ValueError (or a NaN that kills
    the event loop in ``asyncio.wait_for``) on the first MCP call."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MCPConfigError(msgs.MCP_CONFIG_MCP_TIMEOUT_MUST_BE_A_POSITIVE_NUMBER.format(value=value))
    number = float(value)
    if not (math.isfinite(number) and number > 0):
        raise MCPConfigError(msgs.MCP_CONFIG_MCP_TIMEOUT_MUST_BE_A_POSITIVE_NUMBER.format(value=value))
    return number
