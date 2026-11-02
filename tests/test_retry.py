import asyncio

import ai
import pytest

from js import retry


def test_backoff_uses_the_registered_curve():
    budget = retry.Budget.from_settings({
        "RETRY_BACKOFF_START": 3, "RETRY_BACKOFF_MULTIPLIER": 4,
        "RETRY_BACKOFF_MAX": 10, "RETRY_BACKOFF_JITTER": 0,
    })
    assert retry.backoff(1, budget) == 10


def test_default_budget_retries_three_times_eight_seconds_apart(monkeypatch):
    attempts = 0
    waits = []
    failure = ai.ProviderStatusError(
        "upstream failed", is_retryable=True,
        http_context=ai.errors.HTTPErrorContext(status_code=500),
    )

    async def request():
        nonlocal attempts
        attempts += 1
        raise failure

    async def wait(n, budget, delay, exc):
        waits.append(delay)
        assert exc is failure

    monkeypatch.setattr(retry, "wait", wait)
    with pytest.raises(ai.ProviderStatusError) as caught:
        asyncio.run(retry.call(request, retry.Budget.from_settings(None)))

    assert caught.value is failure
    assert attempts == 4
    assert waits == [8, 8, 8]
