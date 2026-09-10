"""Deterministic discovery and loading for turn-scoped tool surfaces."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

from .core import CatalogEntry, Tool, ToolContext
from .descriptions import load_description


DISCOVERY_TOOL_NAME = "tool_discovery"


def discovery_tool(surface: Any) -> Tool:
    """Build the eager discovery tool bound to one turn's lazy surface."""

    async def discover(
        query: str = "",
        kind: str = "",
        source: str = "",
        load: str = "",
        offset: int = 0,
        context: ToolContext | None = None,
    ) -> str:
        return await surface.discover_async(query=query, kind=kind, source=source, load=load, offset=offset)

    return Tool(
        DISCOVERY_TOOL_NAME,
        load_description(DISCOVERY_TOOL_NAME),
        discover,
        {
            "query": {"type": "string", "description": "Intent or words to search; empty returns a compact index."},
            "offset": {"type": "integer", "minimum": 0, "description": "Continue the same search at next_offset from a truncated result."},
            "kind": {"type": "string", "enum": ["native", "skill", "mcp"], "description": "Optional catalog kind filter."},
            "source": {"type": "string", "description": "Optional exact source filter."},
            "load": {"type": "string", "description": "Stable catalog id to load, such as native:browser_probe or skill:review."},
        },
    )


def compact_result(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


# Intent vocabulary supplements metadata without changing tool schemas or policy.
_NATIVE_INTENTS = {
    "fetch": "download url http web",
    "terminal_snapshot": "screenshot image screen capture",
    "kernel": "jupyter notebook python ipython",
    "patch": "edit file modify replace patch",
    "write": "write file create save overwrite",
    "fs_search": "find files filename glob search directory",
    "ast_search": "find code syntax structural search",
    "task": "spawn agent delegate worker subagent",
    "terminal_session": "terminal shell interactive process kill stop terminate",
}


def search_tokens(text: str) -> set[str]:
    """Split punctuation and underscores, never matching kill inside skill."""
    return set(re.findall(r"[^\W_]+", str(text).casefold()))


def ranked_entries(entries: Iterable[CatalogEntry], query: str) -> list[CatalogEntry]:
    terms = search_tokens(query)
    ranked = []
    for item in entries:
        names = search_tokens(item.name)
        intent = search_tokens(_NATIVE_INTENTS.get(item.name, "")) if item.kind == "native" else set()
        metadata = search_tokens(f"{item.id} {item.description} {item.source}")
        matched = terms & (names | intent | metadata)
        if terms and not matched:
            continue
        score = (len(matched), len(terms & names), len(terms & intent))
        ranked.append((score, item))
    return [item for _, item in sorted(ranked, key=lambda pair: (tuple(-n for n in pair[0]), pair[1].id))]


def catalog_result(entries: Iterable[CatalogEntry], query: str, loaded: set[str], offset: int = 0) -> str:
    """Bound serialized UTF-8 bytes while retaining stable ids and pagination."""
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        return "ERROR: offset must be a non-negative integer"
    matches = ranked_entries(entries, query)
    searching = bool(search_tokens(query))
    cap = 8192 if searching else 4096
    result: dict[str, Any] = {"results": [], "total": len(matches), "truncated": False}
    cursor = min(offset, len(matches))
    skipped = 0
    while cursor < len(matches):
        item = matches[cursor]
        row = {"id": item.id, "name": item.name, "kind": item.kind,
               "source": item.source, "loadable": item.loadable, "loaded": item.id in loaded}
        if searching or not item.loadable:
            row["description"] = item.description[:160 if item.loadable else 512]
        # Reserve room for pagination metadata even when identifiers are huge.
        if len(compact_result(row).encode("utf-8")) > cap - 256:
            skipped += 1
            cursor += 1
            continue
        candidate = {**result, "results": [*result["results"], row]}
        if len(compact_result(candidate).encode("utf-8")) > cap - 256 or len(result["results"]) >= 40:
            break
        result["results"].append(row)
        cursor += 1
    if skipped:
        result["omitted_oversized_entries"] = skipped
    if cursor < len(matches):
        result.update(truncated=True, next_offset=cursor)
    if not matches:
        result["hint"] = "Browse with an empty query; narrow with kind or source."
    return compact_result(result)
