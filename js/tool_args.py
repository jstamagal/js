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


_JSON_CONTAINERS = {"array": list, "object": dict}


def coerce_json_containers(value: object, schema: object) -> object:
    """Parse arguments a model serialized as JSON *strings* where its schema
    declares a container.

    Nested arrays and objects are the arguments backends get wrong: llama.cpp's
    grammar path and several tool-call stream parsers hand back
    ``"edits": "[{\\"old_string\\": ...}]"`` — the right JSON inside the wrong
    type. Left alone, the call fails schema validation before the handler is
    ever invoked, so a batch edit is unusable while the scalar form works.

    Only a declared ``array``/``object`` property is touched, only when the
    string parses to exactly that type, and never when ``string`` is also
    allowed. Anything else is returned unchanged, so a genuinely wrong argument
    still fails validation.
    """
    if not isinstance(schema, dict):
        return value
    declared = schema.get("type")
    types = declared if isinstance(declared, list) else [declared]
    if isinstance(value, str) and "string" not in types:
        for name in types:
            expected = _JSON_CONTAINERS.get(name)
            if expected is None:
                continue
            try:
                parsed = json.loads(value)
            except (json.JSONDecodeError, ValueError):
                break
            if isinstance(parsed, expected):
                value = parsed
                break
    if isinstance(value, dict):
        properties = schema.get("properties")
        if isinstance(properties, dict):
            return {
                key: coerce_json_containers(item, properties[key]) if key in properties else item
                for key, item in value.items()
            }
        return value
    if isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, dict):
            return [coerce_json_containers(item, items) for item in value]
    return value


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
