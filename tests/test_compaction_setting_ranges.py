"""`set` refuses a compaction value its reader would replace with the default."""

from __future__ import annotations

import pytest

from js import settings


def _coerce(key: str, raw: str):
    spec = settings.spec_for(key)
    assert spec is not None
    return settings.coerce_value(spec, raw)


@pytest.mark.parametrize("key", [
    "CONTEXT_SIZE_UNKNOWN", "COMPACTION_KEEP_RECENT_TOKENS", "COMPACTION_MIN_SAVINGS_TOKENS",
    "COMPACTION_FAILURES_MAX", "COMPACTION_HINT_TIMEOUT", "COMPACTION_SUMMARY_TOKENS_MAX",
    "COMPACTION_SUMMARY_REQUEST_TOKENS_MAX", "COMPACTION_CONSECUTIVE_MAX",
])
def test_positive_integer_settings_refuse_zero(key):
    value, error = _coerce(key, "0")
    assert error is not None
    assert _coerce(key, "1") == (1, None)


@pytest.mark.parametrize("key", [
    "COMPACTION_HEADROOM_TOKENS", "COMPACTION_REPLY_RESERVE_TOKENS", "COMPACTION_KEEP_TOOL_RESULTS",
    "COMPACTION_OVERFLOW_KEEP_TOOL_RESULTS", "COMPACTION_REATTACH_FILES_MAX",
    "COMPACTION_REATTACH_TOKENS_MAX", "COMPACTION_REATTACH_FILE_TOKENS_MAX",
    "COMPACTION_SUMMARY_RESULT_CHARS_MAX", "COMPACTION_CACHE_TTL",
])
def test_nonnegative_integer_settings_take_zero_and_refuse_negatives(key):
    assert _coerce(key, "0") == (0, None)
    value, error = _coerce(key, "-1")
    assert error is not None


@pytest.mark.parametrize("key", ["COMPACTION_WARN_AT", "COMPACTION_START_AT", "COMPACTION_FORCE_AT"])
@pytest.mark.parametrize("raw, ok", [("0", False), ("-0.1", False), ("0.5", True), ("1", True), ("1.2", False)])
def test_window_fractions_are_above_zero_and_at_most_one(key, raw, ok):
    value, error = _coerce(key, raw)
    assert (error is None) == ok


def test_chars_per_token_must_be_positive():
    assert _coerce("COMPACTION_CHARS_PER_TOKEN", "0")[1] is not None
    assert _coerce("COMPACTION_CHARS_PER_TOKEN", "3.5") == (3.5, None)
