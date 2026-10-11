"""Match routed model names against dated models.dev catalog rows."""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

_EFFORT = re.compile(r"-(off|low|med|medium|high|xhigh|max|minimal)$")
_VARIANTS = {
    "mini", "nano", "super", "ultra", "lite", "image", "tts", "audio",
    "embedding", "embed", "rerank", "safety", "guard", "realtime", "vision",
    "omni", "fast", "highspeed", "ultraspeed", "preview", "experimental",
    "exp", "next", "coder", "coding", "pro", "flash", "max",
}


def name_key(value: str) -> str:
    """Catalog-comparable form of a model name: provider prefix dropped, lower
    case, `_` and `.` folded to `-`. A trailing `:latest` (ollama/router tag
    spelling) folds to `-latest`; any other `:tag` stays."""
    key = value.rsplit("/", 1)[-1].lower()
    if key.endswith(":latest"):
        key = key[: -len(":latest")] + "-latest"
    return key.replace("_", "-").replace(".", "-")


def without_effort(value: str) -> str:
    return _EFFORT.sub("", value)


def family_candidates(request: str, rows: tuple[Any, ...]) -> list[Any]:
    """Keep family and variant identity before comparing release dates."""
    stem = name_key(without_effort(request)).replace("-latest", "")
    requested = set(stem.split("-"))
    matches = []
    for row in rows:
        if not row.release_date or not row.context_window:
            continue
        model = name_key(row.model_id)
        if model.endswith("-latest"):
            # An alias row (a reseller's `claude-opus-latest`) carries a
            # relist/refresh date, not a release date; it never competes.
            continue
        family = name_key(row.family or "")
        words = re.sub(r"\d+(?:-\d+)*", "", model)
        words = re.sub("-+", "-", words).strip("-")
        if not (
            family == stem or family == "gpt-" + stem
            or model == stem or model.startswith(stem + "-")
            or requested <= set(words.split("-"))
        ):
            continue
        if any(char.isdigit() for char in stem) and not (model == stem or model.startswith(stem + "-")):
            continue
        extra = (set(model.split("-")) & _VARIANTS) - requested
        if stem == "step":
            extra -= {"flash"}
        if extra:
            continue
        matches.append(row)
    return matches


def issuer_rank(row: Any) -> int:
    """Prefer the model maker's catalog entry over reseller limits."""
    model = name_key(row.model_id)
    family = (row.family or "").lower()
    provider = row.provider_id.lower()
    issuers = {
        "claude": {"anthropic"}, "gpt": {"openai"}, "grok": {"xai"},
        "gemini": {"google"}, "gemma": {"google"}, "qwen": {"alibaba"},
        "deepseek": {"deepseek"}, "minimax": {"minimax"}, "mimo": {"xiaomi"},
        "nemotron": {"nvidia"}, "step": {"stepfun"}, "laguna": {"poolside"},
        "glm": {"zhipuai", "zai"}, "kimi": {"moonshotai"}, "mistral": {"mistral"},
        "o3": {"openai"}, "o4": {"openai"}, "llama": {"meta"}, "phi": {"microsoft"},
        "command": {"cohere"},
    }
    for prefix, providers in issuers.items():
        if family.startswith(prefix) or model.startswith(prefix):
            return int(provider in providers)
    return int(model.startswith(provider + "-"))


def _date(value: str | None) -> str:
    """A release date padded to YYYY-MM-DD so mixed precision compares right:
    `2026-01` is not newer than `2026-01-15`, and not older than `2025-12-31`."""
    if not value:
        return ""
    parts = value.split("-")
    year = parts[0].zfill(4)
    month = parts[1].zfill(2) if len(parts) > 1 else "01"
    day = parts[2].zfill(2) if len(parts) > 2 else "01"
    return f"{year}-{month}-{day}"


def _pick(rows: list[Any], requested: set[str]) -> Any:
    """Maker first, then newest, then the existing stable order."""
    top = max(issuer_rank(row) for row in rows)
    rows = [row for row in rows if issuer_rank(row) == top]
    newest = max(_date(row.release_date) for row in rows)
    rows = [row for row in rows if _date(row.release_date) == newest]
    return sorted(rows, key=lambda row: (
        -len(requested & set(name_key(row.model_id).split("-"))),
        len(row.model_id), row.provider_id, row.model_id,
    ))[0]


def _select(pool: list[Any], requested: set[str]) -> Any:
    """The row whose limits answer the request.

    The model is the maker's newest row (`_pick`). Its context window is then
    settled among every row in the pool that names that same model id:
    rows with no window abstain; among the rest the top issuer present votes
    alone (a maker's number is never outvoted by resellers); the most common
    window wins and a tie goes to the SMALLER one, because an over-estimated
    window makes compaction fire late and the provider reject the request,
    while an under-estimate only compacts a little early. With no maker row the
    majority among resellers is policy, not truth. The row returned carries
    that window (re-picked among the voters), so callers read it off the row.
    """
    winner = _pick(pool, requested)
    claim_id = name_key(winner.model_id)
    voters = [row for row in pool if name_key(row.model_id) == claim_id and row.context_window]
    if not voters:
        return winner
    top = max(issuer_rank(row) for row in voters)
    voters = [row for row in voters if issuer_rank(row) == top]
    counts = Counter(row.context_window for row in voters)
    most = max(counts.values())
    window = min(value for value, count in counts.items() if count == most)
    return _pick([row for row in voters if row.context_window == window], requested)


def match_routed(request: str, rows: tuple[Any, ...]) -> tuple[Any | None, str]:
    """Catalog row for a routed model name, and how it was matched
    (`normalized`, `effort` or `family-release`).

    1. Rows whose name equals the request (else the request without an effort
       suffix). If the maker has such a row with a window, it answers alone: a
       real id like `gpt-5.2-chat-latest` keeps its own limits even when a
       newer sibling or a reseller relist says otherwise.
    2. A `-latest` request also draws the family (newest release) and lets
       reseller rows of the exact name compete, maker first.
    """
    wanted = name_key(request)
    stripped = name_key(without_effort(request))
    requested = set(stripped.replace("-latest", "").split("-"))
    latest = stripped.endswith("-latest")
    method = "normalized"
    exact = [row for row in rows if name_key(row.model_id) == wanted]
    if not exact and stripped != wanted:
        exact = [row for row in rows if name_key(row.model_id) == stripped]
        method = "effort"
    maker_exact = [row for row in exact if issuer_rank(row) == 1 and row.context_window]
    if maker_exact:
        return _select(maker_exact, requested), method
    pool = list(exact)
    if latest:
        pool += [row for row in family_candidates(stripped, rows) if not any(row is seen for seen in pool)]
    if not pool:
        return None, method
    row = _select(pool, requested)
    return row, (method if any(row is seen for seen in exact) else "family-release")
