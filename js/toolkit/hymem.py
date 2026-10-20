"""memory_search: search the Hy-Memory server's memories of this repository."""

from __future__ import annotations

from .. import hymem
from .core import Tool, ToolContext
from .descriptions import load_description


def memory_search(query: str, limit: int | None = None, context: ToolContext | None = None) -> str:
    assert context is not None
    if not isinstance(query, str) or not query.strip():
        return "ERROR: query is required"
    if not hymem.enabled(context):
        return "ERROR: long-term memory is off (HYMEM_URL is not set)"
    try:
        lines = hymem.search(context, query, context.cwd, limit if isinstance(limit, int) and limit > 0 else None)
    except (OSError, ValueError) as exc:
        return f"ERROR: the memory server did not answer: {exc}"
    return "\n".join(lines) if lines else "(no memories match)"


def tools() -> tuple[Tool, ...]:
    return (
        Tool(
            "memory_search",
            load_description("memory_search"),
            memory_search,
            {
                "query": {
                    "type": "string",
                    "description": "What you want to know, as a question or the task in a sentence.",
                },
                "limit": {"type": "integer", "minimum": 1, "description": "Most memories returned."},
            },
            required=("query",),
            read_only=True,
        ),
    )
