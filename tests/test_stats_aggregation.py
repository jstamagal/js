"""Turn-summary aggregation: shared token denominators and text-anchored ttft."""

from __future__ import annotations

from js.stats import summarize_calls


def test_summarize_calls_sums_token_denominators():
    # A tool-first turn: call 1 has no text token, call 2 does. prompt/cached/output
    # each sum across the turn's calls so they share one denominator, and ttft falls
    # to the first call that actually produced a text token.
    calls = [
        {"prompt_tokens": 1000, "cached_tokens": 0, "output_tokens": 10, "stream_s": 1.0, "ttft_s": None, "finish_reason": "tool_calls"},
        {"prompt_tokens": 6000, "cached_tokens": 5500, "output_tokens": 20, "stream_s": 2.0, "ttft_s": 0.5, "finish_reason": "stop"},
    ]
    row = summarize_calls(calls)
    assert row["prompt_tokens"] == 7000
    assert row["cached_tokens"] == 5500
    assert row["output_tokens"] == 30
    assert row["cached_tokens"] <= row["prompt_tokens"]  # ratio is now meaningful
    assert row["ttft_s"] == 0.5


def test_summarize_calls_ttft_none_when_no_text_at_all():
    assert summarize_calls([{"ttft_s": None, "output_tokens": 0, "stream_s": 0.0}])["ttft_s"] is None
