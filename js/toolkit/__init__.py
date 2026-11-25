"""Modular tool layer for js. The package names the core every tool module
shares; the registry, which imports every tool, is `js.toolkit.registry`."""

from .core import Tool, ToolContext, call_tool

# The context a turn uses when its caller passes none.
STOCK_CONTEXT = ToolContext()

__all__ = [
    "STOCK_CONTEXT",
    "Tool",
    "ToolContext",
    "call_tool",
]
