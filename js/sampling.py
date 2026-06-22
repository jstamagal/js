"""Typed sampling knobs and provider-wire capability mapping."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


_SAMPLING_FIELDS = (
    "temperature",
    "top_p",
    "top_k",
    "repetition_penalty",
    "presence_penalty",
)

_ENV_KEYS = {
    "temperature": "JS_TEMP",
    "top_p": "JS_TOPP",
    "top_k": "JS_TOPK",
    "repetition_penalty": "JS_REPPEN",
    "presence_penalty": "JS_PRPEN",
}

_ANTHROPIC_TRANSPORTS = frozenset({"anthropic", "custom_anthropic"})
_OPENAI_TRANSPORTS = frozenset({"openai", "custom_responses", "codex_oauth"})
_OPENAI_COMPATIBLE_TRANSPORTS = frozenset(
    {
        "openai_compatible",
        "custom_openai",
        "deepseek",
        "ollama",
        "llama.cpp",
        "cliproxyapi",
    }
)

# transport -> (top-level params, extra_body params)
_TRANSPORT_CAPABILITIES: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    **{
        transport: (frozenset({"temperature", "top_p", "top_k"}), frozenset())
        for transport in _ANTHROPIC_TRANSPORTS
    },
    **{
        transport: (
            frozenset({"temperature", "top_p", "presence_penalty"}),
            frozenset(),
        )
        for transport in _OPENAI_TRANSPORTS
    },
    **{
        transport: (
            frozenset({"temperature", "top_p", "presence_penalty"}),
            frozenset({"top_k", "repetition_penalty"}),
        )
        for transport in _OPENAI_COMPATIBLE_TRANSPORTS
    },
}


@dataclass(frozen=True)
class Sampling:
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    repetition_penalty: float | None = None
    presence_penalty: float | None = None

    def merge(self, other: Sampling) -> Sampling:
        """Overlay ``other``'s set fields over this sampling value."""
        values = self.as_dict_nonnull()
        values.update(other.as_dict_nonnull())
        return Sampling(**values)

    @classmethod
    def from_mapping(cls, d: Mapping[str, Any] | None) -> Sampling:
        """Build Sampling from a mapping, coercing known numeric keys.

        Unknown keys are ignored so newer manifests/settings stay forward-compatible
        with older harnesses that do not know a knob yet.
        """
        if not d:
            return cls()
        values: dict[str, float | int | None] = {}
        for key in _SAMPLING_FIELDS:
            if key not in d:
                continue
            raw = d[key]
            if raw is None or raw == "":
                continue
            if key == "top_k":
                values[key] = _coerce_int(key, raw)
            else:
                values[key] = _coerce_float(key, raw)
        return cls(**values)

    @classmethod
    def from_env(cls, env: Mapping[str, Any]) -> Sampling:
        values: dict[str, Any] = {}
        for key, env_name in _ENV_KEYS.items():
            raw = env.get(env_name)
            if raw not in (None, ""):
                values[key] = raw
        return cls.from_mapping(values)

    def is_empty(self) -> bool:
        return not self.as_dict_nonnull()

    def as_dict_nonnull(self) -> dict[str, float | int]:
        return {
            key: value
            for key in _SAMPLING_FIELDS
            if (value := getattr(self, key)) is not None
        }

    def call_params(self, transport: str | None) -> dict[str, Any]:
        return call_params(self, transport)


def _coerce_float(key: str, raw: Any) -> float:
    if isinstance(raw, bool):
        raise ValueError(f"sampling.{key} must be a number")
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"sampling.{key} must be a number") from exc


def _coerce_int(key: str, raw: Any) -> int:
    if isinstance(raw, bool):
        raise ValueError(f"sampling.{key} must be an integer")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        if raw.is_integer():
            return int(raw)
        raise ValueError(f"sampling.{key} must be an integer")
    text = str(raw).strip()
    try:
        return int(text)
    except ValueError:
        try:
            as_float = float(text)
        except ValueError as exc:
            raise ValueError(f"sampling.{key} must be an integer") from exc
        if as_float.is_integer():
            return int(as_float)
        raise ValueError(f"sampling.{key} must be an integer")


def call_params(sampling: Sampling, transport: str | None) -> dict[str, Any]:
    """Return provider-call params supported by the transport's wire family.

    Unknown transports intentionally send no sampling params; model/provider
    defaults win rather than risking an unsupported wire kwarg.
    """
    if sampling.is_empty() or transport is None:
        return {}
    capability = _TRANSPORT_CAPABILITIES.get(transport)
    if capability is None:
        return {}

    top_level_keys, extra_body_keys = capability
    values = sampling.as_dict_nonnull()
    params = {key: values[key] for key in _SAMPLING_FIELDS if key in top_level_keys and key in values}
    extra_body = {key: values[key] for key in _SAMPLING_FIELDS if key in extra_body_keys and key in values}
    if extra_body:
        params["extra_body"] = extra_body
    return params
