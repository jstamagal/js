"""Modular tool layer for js."""

from .core import Tool, ToolContext, call_tool
from .registry import ToolRegistry, build_default_registry

# The registry and context a turn uses when its caller passes none.
STOCK_REGISTRY = build_default_registry()
STOCK_CONTEXT = ToolContext()

__all__ = [
    "STOCK_CONTEXT",
    "STOCK_REGISTRY",
    "Tool",
    "ToolContext",
    "ToolRegistry",
    "build_default_registry",
    "call_tool",
]
