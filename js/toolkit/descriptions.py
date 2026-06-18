"""Markdown-backed model-facing tool descriptions."""
from __future__ import annotations

from functools import cache
from pathlib import Path

_DESCRIPTION_DIR = Path(__file__).with_name("tool_descriptions")


@cache
def load_description(name: str) -> str:
    """Load a non-empty markdown description for a registered tool."""
    path = _DESCRIPTION_DIR / f"{name}.md"
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        raise FileNotFoundError(f"missing tool description: {path}") from None
    if not text:
        raise ValueError(f"empty tool description: {path}")
    return text
