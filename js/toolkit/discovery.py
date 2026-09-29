"""Deterministic discovery and loading for turn-scoped tool surfaces."""

from __future__ import annotations

import json
import math
import re
from collections import Counter
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

# Words that carry no search intent on their own. Without this a query like
# "install a package" matches almost every entry on the single letter "a".
_STOP_WORDS = frozenset({
    "a", "about", "an", "and", "any", "are", "as", "at", "be", "been", "but",
    "by", "can", "could", "did", "do", "does", "for", "from", "had", "has",
    "have", "how", "i", "if", "in", "into", "is", "it", "its", "me", "my",
    "no", "not", "of", "on", "or", "our", "should", "so", "some", "such",
    "than", "that", "the", "their", "them", "then", "there", "these", "they",
    "this", "those", "to", "up", "was", "we", "were", "what", "when", "where",
    "which", "who", "why", "will", "with", "would", "you", "your",
})


def _stem(term: str) -> str:
    """Fold a simple plural to its singular: issues→issue, searches→search."""
    if len(term) > 4 and term.endswith("ies"):
        return term[:-3] + "y"
    if len(term) > 4 and term.endswith(("ches", "shes", "sses", "xes", "zes")):
        return term[:-2]
    if len(term) > 3 and term.endswith("s") and not term.endswith("ss"):
        return term[:-1]
    return term


def tokenize(text: str) -> list[str]:
    """Stemmed terms of ``text`` in order, stop words dropped.

    Splits at camelCase boundaries, underscores and punctuation, so ``kill``
    never matches inside ``skill`` and ``browserProbe`` reads as two words.
    A camelCase word also stays whole after its parts, so ``GitHub`` matches
    ``github``.
    """
    words: list[str] = []
    for word in re.findall(r"[^\W_]+", str(text)):
        spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", word)
        spaced = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", spaced)
        parts = spaced.casefold().split()
        words += parts if len(parts) == 1 else [*parts, word.casefold()]
    return [_stem(token) for token in words if token not in _STOP_WORDS]


def search_tokens(text: str) -> set[str]:
    """The distinct terms of ``text``."""
    return set(tokenize(text))


def query_terms(query: str) -> set[str]:
    """Query terms that carry intent; stop words and single letters carry none."""
    return {token for token in search_tokens(query) if len(token) > 1}


def schema_search_text(schema: Any) -> str:
    """Property names and descriptions of a JSON schema, recursively."""
    parts: list[str] = []

    def walk(node: Any) -> None:
        if not isinstance(node, dict):
            return
        if isinstance(node.get("description"), str):
            parts.append(node["description"])
        properties = node.get("properties")
        if isinstance(properties, dict):
            for name, child in properties.items():
                parts.append(str(name))
                walk(child)
        walk(node.get("items"))
        for key in ("anyOf", "oneOf", "allOf"):
            variants = node.get(key)
            if isinstance(variants, list):
                for variant in variants:
                    walk(variant)

    walk(schema)
    return " ".join(part for part in parts if part.strip())


# Okapi BM25 parameters, the values Codex and pi use.
_BM25_K1 = 1.2
_BM25_B = 0.75
# A name match outweighs the same word in a long description: the name field
# scores separately and counts double.
_NAME_WEIGHT = 2.0


def _name_field(item: CatalogEntry) -> list[str]:
    intent = _NATIVE_INTENTS.get(item.name, "") if item.kind == "native" else ""
    return tokenize(f"{item.name} {intent}")


def _body_field(item: CatalogEntry) -> list[str]:
    return tokenize(f"{item.id} {item.source} {item.description} {item.search_text}")


def _bm25(fields: list[list[str]], terms: list[str]) -> list[float]:
    """Okapi BM25 score of each document in one field for ``terms``."""
    counts = [Counter(field) for field in fields]
    lengths = [len(field) for field in fields]
    average = sum(lengths) / len(fields) or 1.0
    scores = []
    for count, length in zip(counts, lengths):
        norm = _BM25_K1 * (1 - _BM25_B + _BM25_B * length / average)
        score = 0.0
        for term in terms:
            if count[term]:
                df = sum(1 for other in counts if term in other)
                idf = math.log(1 + (len(fields) - df + 0.5) / (df + 0.5))
                score += idf * count[term] * (_BM25_K1 + 1) / (count[term] + norm)
        scores.append(score)
    return scores


def ranked_entries(entries: Iterable[CatalogEntry], query: str) -> list[CatalogEntry]:
    """Entries matching ``query``, best score first, ties by id.

    The score is BM25 over the name (split on ``_`` and camelCase, plus the
    native intent words), weighted double, added to BM25 over the id, source,
    description and schema text. An empty query returns every entry by id.
    """
    items = list(entries)
    if not str(query).strip():
        return sorted(items, key=lambda item: item.id)
    terms = sorted(query_terms(query))
    # A query of nothing but stop words asks for nothing; return no matches
    # rather than the whole catalog.
    if not terms or not items:
        return []
    names = _bm25([_name_field(item) for item in items], terms)
    bodies = _bm25([_body_field(item) for item in items], terms)
    ranked = [
        (_NAME_WEIGHT * name + body, item)
        for name, body, item in zip(names, bodies, items)
        if name + body > 0
    ]
    return [item for _, item in sorted(ranked, key=lambda pair: (-pair[0], pair[1].id))]


def catalog_result(entries: Iterable[CatalogEntry], query: str, loaded: set[str], offset: int = 0) -> str:
    """Bound serialized UTF-8 bytes while retaining stable ids and pagination."""
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        return "ERROR: offset must be a non-negative integer"
    matches = ranked_entries(entries, query)
    searching = bool(str(query).strip())
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
