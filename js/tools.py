"""Compatibility facade for the modular tool layer."""

from __future__ import annotations

from .toolkit import Tool, ToolContext, build_default_registry, call_tool
from .toolkit.registry import ToolRegistry

_REGISTRY = build_default_registry()
TOOLS = _REGISTRY.tools
DEFAULT_CONTEXT = ToolContext()


def resolve(name: str) -> Tool | None:
    """Look up a tool by canonical name or common model alias."""
    return _REGISTRY.resolve(name)


def call(tool: Tool, args: dict, context: ToolContext | None = None) -> str:
    """Invoke a tool using the shared runtime context by default."""
    return call_tool(tool, args, context or DEFAULT_CONTEXT)

def call_with_context(tool: Tool, args: dict, context: ToolContext) -> str:
    """Invoke a tool with an explicit isolated runtime context."""
    return call_tool(tool, args, context)


def select(selectors) -> ToolRegistry:
    """Build a registry containing only the selected tools."""
    return _REGISTRY.select(selectors)


def openai_specs() -> list[dict]:
    return _REGISTRY.openai_specs()


def tool_names() -> str:
    return _REGISTRY.names()
