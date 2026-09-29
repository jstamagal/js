"""Modular tool layer for js."""

from .core import Tool, ToolContext, call_tool
from .registry import ToolRegistry, build_default_registry

# The registry and context a turn uses when its caller passes none.
DEFAULT_REGISTRY = build_default_registry()
DEFAULT_CONTEXT = ToolContext()

__all__ = [
    "DEFAULT_CONTEXT",
    "DEFAULT_REGISTRY",
    "Tool",
    "ToolContext",
    "ToolRegistry",
    "build_default_registry",
    "call_tool",
]
