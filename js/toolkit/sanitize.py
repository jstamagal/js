"""Small coercion helpers for user/model-supplied tool parameters."""

from __future__ import annotations

from typing import Any


def text_or_default(raw: Any, default: str = "") -> str:
    if raw is None or isinstance(raw, bool):
        return default
    return str(raw)


def int_or_default(raw: Any, default: int, *, minimum: int | None = None) -> int:
    if raw is None or isinstance(raw, bool):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    if minimum is not None and value < minimum:
        return default
    return value
