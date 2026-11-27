"""Scrubbing credentials out of data an MCP server controls."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def redact_value(value: Any, redact_text: Callable[[str], str]) -> Any:
    """``value`` with every string in it, keys included, passed through
    ``redact_text``: dicts, lists and tuples are walked, anything else is
    returned as it is."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {redact_text(key) if isinstance(key, str) else key: redact_value(item, redact_text)
                for key, item in value.items()}
    if isinstance(value, list):
        return [redact_value(item, redact_text) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item, redact_text) for item in value)
    return value
