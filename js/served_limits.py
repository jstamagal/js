"""Reading a model's limits out of what a server says about it: the field
names servers use, and the numbers under them."""

from __future__ import annotations

from typing import Any

SERVED_CONTEXT_KEYS = {
    "context_length",
    "context_window",
    "max_context_length",
    "max_context_window",
    "max_model_len",
    "max_model_length",
    "max_position_embeddings",
    "max_seq_len",
    "max_sequence_length",
    "max_input_tokens",
    "n_ctx",
    "num_ctx",
}


def positive_int(value: Any) -> int | None:
    """``value`` as a whole number above zero: an int, a float that is one,
    or digits in a string (`_` allowed between them); None otherwise."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        return int(value) if value.is_integer() and value > 0 else None
    if isinstance(value, str):
        normalized = value.strip().replace("_", "")
        if normalized.isdigit():
            parsed = int(normalized)
            return parsed if parsed > 0 else None
    return None


def _key_matches(key: Any, names: set[str]) -> bool:
    lowered = str(key).lower()
    return lowered in names or any(lowered.endswith(f".{name}") for name in names)


def find_positive_int(payload: Any, names: set[str]) -> int | None:
    """The first `positive_int` under a key named in ``names`` (or ending
    `.name`) anywhere in ``payload``: a mapping's own keys before what is
    nested in its values."""
    if isinstance(payload, dict):
        for key, value in payload.items():
            if _key_matches(key, names):
                parsed = positive_int(value)
                if parsed is not None:
                    return parsed
        for value in payload.values():
            parsed = find_positive_int(value, names)
            if parsed is not None:
                return parsed
    elif isinstance(payload, list):
        for item in payload:
            parsed = find_positive_int(item, names)
            if parsed is not None:
                return parsed
    return None
