"""Exact model-facing text catalogs shipped beside the agent prompts."""
from __future__ import annotations

import json
from functools import cache
from pathlib import Path


@cache
def load_catalog(name: str) -> dict[str, str]:
    """Load a shared catalog; assemblies can replace entries before importing parts.

    Values preserve whitespace. Only callers of templates with replacement fields
    apply ``str.format``; field values are inserted once, without expansion.
    """
    path = Path(__file__).resolve().parent.parent / "prompts" / "text" / f"{name}.json"
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)
