"""The compaction settings a run reads, each with its jsrc default when the value is unset or out of range."""

from __future__ import annotations

from typing import Any

from . import settings as _settings
from .config import Config

# --------------------------------------------------------------------------
# compact.* settings readers — the one family
# --------------------------------------------------------------------------


# A reader called without a default falls back to the knob's js/jsrc value.


# A reader called without a default falls back to the knob's js/jsrc value.
_JSRC = object()


def _fallback(key: str, default: Any) -> Any:
    return _settings.default_value(key) if default is _JSRC else default


def get(cfg: Config, key: str, default: Any = _JSRC) -> Any:
    """Setting ``key`` (`COMPACTION_AUTO`) from ``cfg.settings``."""
    default = _fallback(key, default)
    settings = getattr(cfg, "settings", {}) or {}
    return settings.get(key, default) if isinstance(settings, dict) else default


def get_bool(cfg: Config, key: str, default: Any = _JSRC) -> bool:
    """A boolean knob. Garbage reads as the default, and a default that is not
    a bool itself (a bad jsrc value) reads as False, so the result is a bool."""
    default = _fallback(key, default)
    value = get(cfg, key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return default if isinstance(default, bool) else False


def get_int(cfg: Config, key: str, default: Any = _JSRC, *, max_value: int | None = None) -> int:
    default = _fallback(key, default)
    raw = get(cfg, key, default)
    if isinstance(raw, bool):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    if value <= 0:
        return default
    if max_value is not None:
        return min(value, max_value)
    return value


def get_nonnegative_int(cfg: Config, key: str) -> int:
    """Like get_int but 0 is a legal value, not a request for the default —
    a buffer of 0 is a real choice."""
    default = _settings.default_value(key)
    raw = get(cfg, key, default)
    if isinstance(raw, bool):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    if value < 0:
        return default
    return value


def get_float(cfg: Config, key: str, *, max_value: float | None = None) -> float:
    default = _settings.default_value(key)
    raw = get(cfg, key, default)
    if isinstance(raw, bool):
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if value <= 0:
        return default
    if max_value is not None and value > max_value:
        return default
    return value


def get_model(cfg: Config) -> str:
    raw = get(cfg, "COMPACTION_EXTERNAL_MODEL")
    if not isinstance(raw, str):
        return cfg.model
    model = raw.strip()
    if not model or model.lower() == "same":
        return cfg.model
    return model
