"""Modular tool layer for js."""

from .core import Tool, ToolContext, call_tool
from .registry import ToolRegistry, build_default_registry, registry_for_roots
from .. import paths

# The context a turn uses when its caller passes none.
STOCK_CONTEXT = ToolContext()


def stock_registry() -> ToolRegistry:
    """Resolve the fallback tool surface after the invocation selects its home."""
    return registry_for_roots(paths.agent_roots())

__all__ = [
    "STOCK_CONTEXT",
    "stock_registry",
    "Tool",
    "ToolContext",
    "ToolRegistry",
    "build_default_registry",
    "call_tool",
]
