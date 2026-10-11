"""Helpers for model-emitted tool-call argument JSON."""

from __future__ import annotations

import json

from .toolkit.core import compact_json


def _strip_commas_outside_strings(text: str) -> str:
    """Drop `,` that sit outside any string, directly before `}` or `]`.

    One scan tracking whether the cursor is inside a string and whether the
    previous backslash escaped the current character, so a `, }` that is part
    of a string VALUE is never touched.
    """
    out: list[str] = []
    in_string = False
    escaped = False
    length = len(text)
    for index, char in enumerate(text):
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            out.append(char)
            continue
        if char == ",":
            lookahead = index + 1
            while lookahead < length and text[lookahead] in " \t\r\n":
                lookahead += 1
            if lookahead < length and text[lookahead] in "}]":
                continue  # drop the comma and its trailing whitespace
            out.append(char)
            continue
        out.append(char)
    return "".join(out)


def _close_object(text: str) -> str | None:
    """`{"a": 1,` with one missing closing brace, else None."""
    stripped = text.strip()
    if stripped.startswith("{") and not stripped.endswith("}"):
        return stripped + "}"
    return None


def repair_jsonish(raw: str) -> dict:
    """Best-effort repair for common model-emitted argument JSON."""
    if not raw:
        return {}
    stripped = raw.strip()
    candidates = [raw, stripped]
    if stripped.startswith('"') and stripped.endswith('"'):
        try:
            decoded = json.loads(stripped)
            if isinstance(decoded, str):
                # Unwrap BEFORE scanning: the encoded form hides its commas
                # behind escapes, so stripping must run on the decoded text.
                candidates.extend([decoded, decoded.strip()])
        except json.JSONDecodeError:
            pass
    # Structure-only comma strip on every candidate, and the closing brace
    # both before and after the strip, so `{"a": 1,` and `{"a": 1,}` both mend.
    for item in list(candidates):
        closed = _close_object(item)
        if closed is not None:
            candidates.append(closed)
    candidates.extend(_strip_commas_outside_strings(item) for item in list(candidates))
    for item in list(candidates):
        closed = _close_object(item)
        if closed is not None:
            candidates.append(closed)
    last_error: Exception | None = None
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            if not isinstance(parsed, dict):
                raise ValueError(f"tool args must be an object, got {type(parsed).__name__}")
            return parsed
        except (json.JSONDecodeError, ValueError, RecursionError) as exc:
            last_error = exc
    raise ValueError(str(last_error) if last_error else "could not parse arguments")


_JSON_CONTAINERS = {"array": list, "object": dict}


def _scalar_from_string(value: str, types: list) -> object:
    """``value`` parsed as the one scalar type its schema declares, or
    unchanged when it does not parse as that type."""
    if "integer" in types:
        try:
            return int(value.strip())
        except ValueError:
            pass
    if "number" in types:
        try:
            return float(value.strip())
        except ValueError:
            pass
    if "boolean" in types:
        lowered = value.strip().lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
    return value


def coerce_to_schema(value: object, schema: object) -> object:
    """Convert arguments a model serialized as *strings* to the type its
    schema declares.

    Two shapes of the same mistake: a backend hands back
    ``"edits": "[{\"old_string\": ...}]"`` (llama.cpp's grammar path and
    several tool-call stream parsers) or ``"timeout": "15"`` — the right value
    in the wrong type. Left alone, the call fails schema validation before the
    handler is ever invoked.

    Only a property whose declared type excludes ``string`` is touched: a
    container only when the string parses to exactly that container, a scalar
    only when the string parses as that integer, number or boolean (``"1.5"``
    is not an integer; ``"yes"`` is not a boolean). Anything else is returned
    unchanged, so a genuinely wrong argument still fails validation.
    """
    if not isinstance(schema, dict):
        return value
    declared = schema.get("type")
    types = list(declared) if isinstance(declared, list) else [declared]
    # `anyOf`/`oneOf` branches count as declared types: a stringified object
    # under anyOf[object, array] would otherwise stay a string and fail the
    # very validation this coercion exists to pass. A branch that declares
    # string means the string is already valid - leave it alone. `$ref` is
    # left unresolved (no resolver lives in this file).
    def _collect(schema_part: dict) -> None:
        branch_type = schema_part.get("type")
        if isinstance(branch_type, list):
            types.extend(branch_type)
        elif branch_type is not None:
            types.append(branch_type)
        for combiner in ("anyOf", "oneOf"):
            branches = schema_part.get(combiner)
            if isinstance(branches, list):
                for branch in branches:
                    if isinstance(branch, dict) and "$ref" not in branch:
                        _collect(branch)

    for combiner in ("anyOf", "oneOf"):
        branches = schema.get(combiner)
        if isinstance(branches, list):
            for branch in branches:
                if isinstance(branch, dict) and "$ref" not in branch:
                    _collect(branch)
    seen_types: set = set()
    types = [t for t in types if not (t in seen_types or seen_types.add(t))]
    if isinstance(value, str) and "string" not in types:
        for name in types:
            expected = _JSON_CONTAINERS.get(name)
            if expected is None:
                continue
            try:
                parsed = json.loads(value)
            except (json.JSONDecodeError, ValueError, RecursionError):
                break
            if isinstance(parsed, expected):
                value = parsed
                break
        if isinstance(value, str):
            value = _scalar_from_string(value, types)
    if isinstance(value, dict):
        properties = schema.get("properties")
        if isinstance(properties, dict):
            return {
                key: coerce_to_schema(item, properties[key]) if key in properties else item
                for key, item in value.items()
            }
        return value
    if isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, dict):
            return [coerce_to_schema(item, items) for item in value]
    return value


def is_json_object(raw: str) -> bool:
    try:
        return isinstance(json.loads(raw), dict)
    except (json.JSONDecodeError, TypeError, RecursionError):
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
    except (ValueError, TypeError, RecursionError):
        return raw


def sdk_safe_tool_args(raw: str) -> str:
    """Return args that the SDK integrity pass will accept without warnings."""
    fixed = canonical_tool_args(raw)
    return fixed if is_json_object(fixed) else "{}"


def schema_error(arguments: object, schema: object) -> str | None:
    """One line saying how ``arguments`` break ``schema``; None when they fit
    or there is no schema."""
    if not isinstance(schema, dict):
        return None
    from jsonschema import exceptions as jsonschema_exceptions
    from jsonschema import validators as jsonschema_validators

    try:
        validator_type = jsonschema_validators.validator_for(schema)
        validator_type.check_schema(schema)
        validator_type(schema).validate(arguments)
    except (jsonschema_exceptions.SchemaError, jsonschema_exceptions.ValidationError) as exc:
        return " ".join(exc.message.split())
    return None
