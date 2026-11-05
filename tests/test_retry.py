import asyncio

import ai
import httpx2
import pytest

from js import messages as msgs, model_client, network_display, output, retry


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
        http_context=ai.errors.HTTPErrorContext(status_code=503),
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


def _status(code: int, error_type: str | None = None) -> ai.ProviderStatusError:
    body = {"type": "error", "error": {"type": error_type, "message": "m"}} if error_type else None
    return ai.ProviderStatusError("failed", body=body, http_context=ai.errors.HTTPErrorContext(status_code=code))


@pytest.mark.parametrize(("code", "retried"), [
    (429, True), (502, True), (503, True), (504, True), (529, True),
    (500, False), (400, False), (401, False), (404, False), (408, False), (409, False),
])
def test_only_429_gateway_and_overloaded_statuses_are_retried(code, retried):
    assert model_client.retryable(_status(code)) is retried


@pytest.mark.parametrize(("error_type", "retried"), [
    ("rate_limit_error", True), ("timeout_error", True), ("overloaded_error", True), ("api_error", False),
])
def test_an_error_event_in_a_200_stream_is_retried_as_its_type_status(error_type, retried):
    assert model_client.retryable(_status(200, error_type)) is retried


@pytest.mark.parametrize(("code", "retried"), [
    ("rate_limit_exceeded", True), ("server_error", False), ("internal_error", False), ("model_error", False),
    ("context_length_exceeded", False),
])
def test_a_responses_stream_code_is_retried_as_the_status_it_stands_for(code, retried):
    assert model_client.retryable(ai.ProviderAPIError("failed", code=code)) is retried


def test_a_connection_lost_is_retried():
    assert model_client.retryable(ai.ProviderConnectionError("Connection error."))
    assert model_client.retryable(ai.ProviderTimeoutError("Request timed out."))


def test_an_answer_with_no_status_is_not_retried():
    assert not model_client.retryable(ai.ProviderResponseError("tool arguments do not match their deltas"))


def test_js_clocks_say_for_themselves():
    assert model_client.retryable(model_client.StreamIdleError(300))
    assert not model_client.retryable(model_client.FirstByteTimeoutError(60))
    assert model_client.retryable(model_client.ConnectTimeoutError(15))


def test_a_handshake_past_connect_timeout_names_the_setting():
    try:
        try:
            raise httpx2.ConnectTimeout("timed out")
        except httpx2.ConnectTimeout as exc:
            raise ai.ProviderTimeoutError("Request timed out.") from exc
    except ai.ProviderTimeoutError as exc:
        failure = model_client._request_failure(
            exc, provider="openai", limits=retry.StreamLimits(connect=15.0))

    assert isinstance(failure, model_client.ConnectTimeoutError) and failure.is_retryable
    assert "CONNECT_TIMEOUT" in str(failure)


def test_stream_limits_read_the_settings_and_zero_is_no_limit():
    limits = retry.StreamLimits.from_settings(
        {"CONNECT_TIMEOUT": 30, "FIRST_BYTE_TIMEOUT": 0, "STREAM_IDLE_TIMEOUT": 120})
    assert limits == retry.StreamLimits(connect=30.0, first_byte=None, idle=120.0)


def test_giving_up_names_the_last_failure():
    events = []

    class _Sink:
        def emit(self, event):
            events.append(event)

    async def request():
        raise _status(503)

    with output.using_sink(_Sink()):
        with pytest.raises(ai.ProviderStatusError):
            asyncio.run(retry.call(request, retry.Budget(attempts=0, max_wait=0)))

    gave_up = next(event for event in events if event.name == "retry_gave_up")
    error = dict(gave_up.fields)["error"]
    line = msgs.RETRY_GAVE_UP.words(n=0, failure=network_display.describe_failure(error))
    assert "503" in line and "\x1b" not in line
