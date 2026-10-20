from js import retry


def test_backoff_uses_the_registered_curve():
    budget = retry.Budget.from_settings({
        "RETRY_BACKOFF_START": 3, "RETRY_BACKOFF_MULTIPLIER": 4,
        "RETRY_BACKOFF_MAX": 10, "RETRY_BACKOFF_JITTER": 0,
    })
    assert retry.backoff(1, budget) == 10
