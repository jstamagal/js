"""Read the model manifest the codex CLI caches from OpenAI's server.

models.dev carries one row per model id, describing the *public API* surface.
The ChatGPT/codex subscription serves the same ids from a different pool with a
different window — gpt-5.6-sol is 1,050,000 on the API and 272,000 on codex —
and models.dev has no openai-codex provider row at all, so there is nothing in
that catalog to correct.

The codex CLI already solves this: its models-manager fetches a manifest from
the server and caches it at ~/.codex/models_cache.json, keyed by model slug.
That file is the server's own answer for this account, it refreshes itself when
codex runs, and reading it costs one stat. Preferring it over the catalog for
openai-codex models is the difference between a hand-maintained constant that
goes stale and a number that tracks the account.

`effective_context_window_percent` is codex's own headroom policy (95% of the
window is usable). We keep the raw window and let compact.buffer_tokens play
that role, rather than applying the discount twice.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# Slug -> context window, plus the mtime/size the map was built from.
_CACHE: dict[str, int] = {}
_CACHE_STAMP: tuple[float, int] | None = None
_MISSING = (0.0, -1)


def manifest_path() -> Path:
    override = os.environ.get("CODEX_HOME")
    root = Path(override).expanduser() if override else Path.home() / ".codex"
    return root / "models_cache.json"


def _stamp(path: Path) -> tuple[float, int]:
    try:
        st = path.stat()
    except OSError:
        return _MISSING
    return (st.st_mtime, st.st_size)


def _parse(raw: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    models = raw.get("models") if isinstance(raw, dict) else None
    if not isinstance(models, list):
        return out
    for entry in models:
        if not isinstance(entry, dict):
            continue
        slug = entry.get("slug")
        if not isinstance(slug, str) or not slug.strip():
            continue
        # context_window is what this account may actually send; max_context_window
        # is the model's ceiling and can be far larger (gpt-5.4: 272k vs 1M).
        window = entry.get("context_window")
        try:
            window = int(window)
        except (TypeError, ValueError):
            continue
        if window > 0:
            out[slug.strip().lower()] = window
    return out


def context_window(model_id: str) -> int | None:
    """Window for ``model_id`` per the cached codex manifest, or None.

    Re-reads only when the file's mtime/size changed, so a codex run that
    refreshes the manifest is picked up without restarting js.
    """
    global _CACHE_STAMP
    path = manifest_path()
    stamp = _stamp(path)
    if stamp != _CACHE_STAMP:
        _CACHE_STAMP = stamp
        _CACHE.clear()
        if stamp != _MISSING:
            try:
                _CACHE.update(_parse(json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, ValueError):
                pass  # a truncated or half-written cache must not break resolution
    if not _CACHE:
        return None
    return _CACHE.get((model_id or "").strip().lower())
