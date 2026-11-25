import math

import pytest

from js import retry


@pytest.mark.parametrize(("attempt", "jitter", "expected"), [
    (0, 0, 2), (0, 1.5, 3.5), (1, 5, 8),
    (2, 0, 8), (2, 5, 8), (5, 5, 8),
])
def test_backoff_caps_final_jittered_delay(monkeypatch, attempt, jitter, expected):
    monkeypatch.setattr(retry.random, "uniform", lambda low, high: jitter)
    budget = retry.Budget(attempts=10, max_wait=0, backoff_start=2,
                          backoff_multiplier=2, backoff_max=8, backoff_jitter=5)
    assert retry.backoff(attempt, budget) == expected


@pytest.mark.parametrize("header", ["retry-after-ms", "retry-after"])
@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "NaN", "Infinity"])
def test_nonfinite_retry_after_reaches_budget_guard(header, value):
    error = RuntimeError("temporary failure")
    error.headers = {header: value}
    delay = retry.retry_after_seconds(error)
    assert delay is not None and not math.isfinite(delay)
    assert retry.Budget(attempts=3, max_wait=0).too_long(delay)


@pytest.mark.parametrize(("headers", "expected"), [
    ({"retry-after-ms": "2500"}, 2.5),
    ({"retry-after-ms": "-2500"}, 0),
    ({"retry-after": "2.5"}, 2.5),
    ({"retry-after": "-2.5"}, 0),
    ({"retry-after-ms": "bad", "retry-after": "3"}, 3),
    ({"retry-after-ms": "bad", "retry-after": "bad"}, None),
])
def test_finite_retry_after_and_fallback(headers, expected):
    error = RuntimeError("temporary failure")
    error.headers = headers
    assert retry.retry_after_seconds(error) == expected
