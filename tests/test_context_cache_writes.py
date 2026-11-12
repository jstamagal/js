"""Provider cache-write accounting anchors the full prompt, not just fresh input."""

import pytest
from ai.types.usage import Usage

from js.context_budget import TokenState, estimate_request_tokens, usage_from_provider


@pytest.mark.parametrize("fresh", [0, 1200])
def test_anthropic_cold_cache_counts_write_tokens_in_context(fresh):
    usage = Usage(input_tokens=fresh, cache_write_tokens=150000, output_tokens=300,
                  raw={"cache_creation_input_tokens": 150000})
    messages = [{"role": "user", "content": "task"}]
    state = TokenState()
    state.record_provider_usage(usage, message_count=1, messages=messages)

    status = state.budget_status(messages=messages, context_window=128000)

    assert state.last_usage.input_tokens == fresh + 150000
    assert state.last_usage.cache_write_tokens == 150000
    assert status.current_context_tokens == fresh + 150300
    assert status.used_provider_usage
    assert status.should_compact


def test_anthropic_warm_cache_does_not_double_count_read_tokens():
    usage = Usage(input_tokens=151200, cache_read_tokens=150000,
                  cache_write_tokens=200, output_tokens=300,
                  raw={"cache_creation_input_tokens": 200, "cache_read_input_tokens": 150000})
    normalized = usage_from_provider(usage)
    assert normalized.input_tokens == 151400
    assert normalized.cache_read_tokens == 150000
    assert normalized.cache_write_tokens == 200
    assert normalized.total_tokens == 151700
    assert usage_from_provider(normalized) == normalized


@pytest.mark.parametrize("raw", [None, {}, {"prompt_tokens": 151200}])
def test_inclusive_sdk_input_does_not_add_cache_writes_again(raw):
    usage = Usage(input_tokens=151200, cache_read_tokens=100000,
                  cache_write_tokens=50000, output_tokens=300, raw=raw)
    normalized = usage_from_provider(usage)
    assert normalized.input_tokens == 151200
    assert normalized.total_tokens == 151500


def test_cold_cache_calibration_uses_full_normalized_prompt():
    messages = [{"role": "user", "content": "words " * 100}]
    estimate = estimate_request_tokens(messages=messages).total_tokens
    state = TokenState()
    usage = Usage(input_tokens=10, cache_write_tokens=estimate - 10, output_tokens=0,
                  raw={"cache_creation_input_tokens": estimate - 10})
    state.record_provider_usage(usage, message_count=1, messages=messages)
    assert state.calibrated_chars_per_token(messages=messages) == pytest.approx(4.0)
