"""Helpers for model-emitted tool-call argument JSON."""

from __future__ import annotations

import json
import re

from .toolkit.core import compact_json


_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")


def repair_jsonish(raw: str) -> dict:
    """Best-effort repair for common model-emitted argument JSON."""
    if not raw:
        return {}
    candidates = [raw, raw.strip()]
    stripped = raw.strip()
    if stripped.startswith('"') and stripped.endswith('"'):
        try:
            decoded = json.loads(stripped)
            if isinstance(decoded, str):
                candidates.append(decoded)
        except json.JSONDecodeError:
            pass
    candidates.extend(_TRAILING_COMMA_RE.sub(r"\1", item) for item in list(candidates))
    if stripped and stripped.startswith("{") and not stripped.endswith("}"):
        candidates.append(stripped + "}")
    last_error: Exception | None = None
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            if not isinstance(parsed, dict):
                raise ValueError(f"tool args must be an object, got {type(parsed).__name__}")
            return parsed
        except (json.JSONDecodeError, ValueError) as exc:
            last_error = exc
    raise ValueError(str(last_error) if last_error else "could not parse arguments")


def is_json_object(raw: str) -> bool:
    try:
        return isinstance(json.loads(raw), dict)
    except (json.JSONDecodeError, TypeError):
        return False


def canonical_tool_args(raw: str) -> str:
    """Return tool-call args as a JSON object string when repair is possible.

    Valid object JSON is preserved byte-for-byte. Repairable malformed args are
    normalized to compact JSON. Unrepairable args are returned unchanged so the
    caller can choose whether to keep, drop, or replace them.
    """
    if not raw:
        return raw
    if is_json_object(raw):
        return raw
    try:
        return compact_json(repair_jsonish(raw))
    except (ValueError, TypeError):
        return raw


def sdk_safe_tool_args(raw: str) -> str:
    """Return args that the SDK integrity pass will accept without warnings."""
    fixed = canonical_tool_args(raw)
    return fixed if is_json_object(fixed) else "{}"
