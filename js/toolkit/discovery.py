"""Deterministic discovery and loading for turn-scoped tool surfaces."""

from __future__ import annotations

import json
from typing import Any

from .core import Tool, ToolContext
from .descriptions import load_description


DISCOVERY_TOOL_NAME = "tool_discovery"


def discovery_tool(surface: Any) -> Tool:
    """Build the eager discovery tool bound to one turn's lazy surface."""

    async def discover(
        query: str = "",
        kind: str = "",
        source: str = "",
        load: str = "",
        context: ToolContext | None = None,
    ) -> str:
        return await surface.discover_async(query=query, kind=kind, source=source, load=load)

    return Tool(
        DISCOVERY_TOOL_NAME,
        load_description(DISCOVERY_TOOL_NAME),
        discover,
        {
            "query": {"type": "string", "description": "Words to find in catalog names and descriptions."},
            "kind": {"type": "string", "enum": ["native", "skill", "mcp"], "description": "Optional catalog kind filter."},
            "source": {"type": "string", "description": "Optional exact source filter."},
            "load": {"type": "string", "description": "Stable catalog id to load, such as native:browser_open or skill:review."},
        },
    )


def compact_result(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
