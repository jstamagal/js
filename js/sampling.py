"""Typed sampling settings and where each one goes in a request."""

from __future__ import annotations

from . import messages as msgs

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Any


@dataclass(frozen=True)
class Sampling:
    """The sampling settings one model call sends. A field left None is not
    sent."""

    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    min_p: float | None = None
    top_n_sigma: float | None = None
    xtc_probability: float | None = None
    xtc_threshold: float | None = None
    dry_multiplier: float | None = None
    dry_base: float | None = None
    dry_allowed_length: int | None = None
    dry_penalty_last_n: int | None = None
    dry_sequence_breakers: tuple[str, ...] | None = None
    repetition_penalty: float | None = None
    presence_penalty: float | None = None

    def merge(self, other: Sampling) -> Sampling:
        """Overlay ``other``'s set fields over this sampling value."""
        values = self.as_dict_nonnull()
        values.update(other.as_dict_nonnull())
        return Sampling.from_mapping(values)

    @classmethod
    def from_mapping(cls, d: Mapping[str, Any] | None) -> Sampling:
        """Build Sampling from a mapping of field names. A key that is not a
        field is not read; None or "" leaves the field unset."""
        if not d:
            return cls()
        values: dict[str, Any] = {}
        for key in SAMPLING_FIELDS:
            if key not in d:
                continue
            raw = d[key]
            if raw is None or raw == "":
                continue
            if key in _INT_FIELDS:
                values[key] = _coerce_int(key, raw)
            elif key in _LIST_FIELDS:
                values[key] = _coerce_strings(key, raw)
            else:
                values[key] = _coerce_float(key, raw)
        return cls(**values)

    @classmethod
    def from_env(cls, env: Mapping[str, Any]) -> Sampling:
        values: dict[str, Any] = {}
        for key, env_names in _ENV_KEYS.items():
            for env_name in env_names:
                raw = env.get(env_name)
                if raw not in (None, ""):
                    values[key] = raw
                    break
        return cls.from_mapping(values)

    def is_empty(self) -> bool:
        return not self.as_dict_nonnull()

    def as_dict_nonnull(self) -> dict[str, Any]:
        return {
            key: value
            for key in SAMPLING_FIELDS
            if (value := getattr(self, key)) is not None
        }

    def split(self, standard: frozenset[str]) -> tuple[dict[str, Any], dict[str, Any]]:
        """The set fields as (the ``standard`` ones, every other one). The
        caller sends the first as the wire's own parameters and the second in
        ``extra_body``. Unset fields are in neither."""
        values = self.as_dict_nonnull()
        top = {key: value for key, value in values.items() if key in standard}
        extra = {key: (list(value) if isinstance(value, tuple) else value)
                 for key, value in values.items() if key not in standard}
        return top, extra


SAMPLING_FIELDS: tuple[str, ...] = tuple(field.name for field in fields(Sampling))
_INT_FIELDS = frozenset({"top_k", "dry_allowed_length", "dry_penalty_last_n"})
_LIST_FIELDS = frozenset({"dry_sequence_breakers"})

# Each sampling field's env var: JS_<SETTING>, the setting being the field
# name in capitals (TEMPERATURE -> JS_TEMPERATURE).
_ENV_KEYS = {field: ("JS_" + field.upper(),) for field in SAMPLING_FIELDS}


def _coerce_float(key: str, raw: Any) -> float:
    if isinstance(raw, bool):
        raise ValueError(msgs.SAMPLING_SAMPLING_MUST_BE_A_NUMBER.words(key=key))
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(msgs.SAMPLING_SAMPLING_MUST_BE_A_NUMBER.words(key=key)) from exc
    if not math.isfinite(value):  # nan/inf serialize as invalid JSON
        raise ValueError(msgs.SAMPLING_SAMPLING_MUST_BE_A_FINITE_NUMBER.words(key=key))
    return value


def _coerce_int(key: str, raw: Any) -> int:
    if isinstance(raw, bool):
        raise ValueError(msgs.SAMPLING_SAMPLING_MUST_BE_AN_INTEGER.words(key=key))
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        if raw.is_integer():
            return int(raw)
        raise ValueError(msgs.SAMPLING_SAMPLING_MUST_BE_AN_INTEGER.words(key=key))
    text = str(raw).strip()
    try:
        return int(text)
    except ValueError:
        try:
            as_float = float(text)
        except ValueError as exc:
            raise ValueError(msgs.SAMPLING_SAMPLING_MUST_BE_AN_INTEGER.words(key=key)) from exc
        if as_float.is_integer():
            return int(as_float)
        raise ValueError(msgs.SAMPLING_SAMPLING_MUST_BE_AN_INTEGER.words(key=key))


def _coerce_strings(key: str, raw: Any) -> tuple[str, ...]:
    """A JSON list of strings, or that list as JSON text."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise ValueError(msgs.SAMPLING_MUST_BE_A_LIST_OF_STRINGS.words(key=key)) from exc
    if not isinstance(raw, (list, tuple)) or any(not isinstance(item, str) for item in raw):
        raise ValueError(msgs.SAMPLING_MUST_BE_A_LIST_OF_STRINGS.words(key=key))
    return tuple(raw)


def _template_entries(text: str) -> list[str]:
    """Split top-level commas while preserving JSON containers and strings."""
    entries = []
    start = depth = 0
    quoted = escaped = False
    for index, char in enumerate(text):
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            entries.append(text[start:index])
            start = index + 1
    entries.append(text[start:])
    return entries


def parse_template_kwargs(text: str) -> dict[str, Any]:
    """``key=value`` entries split by ',' (`TEMPLATE_KWARGS`, `--template-kwargs`)
    as the request's ``chat_template_kwargs``. A value is read as JSON
    (``false``, ``1``, ``"x"``); a value that is not JSON is that string.
    Raises ValueError naming the first entry that is not ``key=value`` or a
    key given twice."""
    out: dict[str, Any] = {}
    for entry in _template_entries(text):
        key, sep, raw = entry.partition("=")
        key, raw = key.strip(), raw.strip()
        if not sep or not key or not raw:
            raise ValueError(msgs.EXPECTED_TEMPLATE_KWARGS.said(entry=entry.strip()))
        if key in out:
            raise ValueError(msgs.TEMPLATE_KWARGS_TWICE.said(key=key))
        try:
            out[key] = json.loads(raw)
        except ValueError:
            out[key] = raw
        try:
            json.dumps(out[key], allow_nan=False)
        except ValueError as exc:
            raise ValueError(msgs.SAMPLING_SAMPLING_MUST_BE_A_FINITE_NUMBER.words(
                key=f"template_kwargs.{key}",
            )) from exc
    return out
