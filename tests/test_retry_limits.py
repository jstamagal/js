import math

import pytest

from js import retry


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
