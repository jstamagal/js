"""Outage diagnostics name only services identified by the failure."""

import ai
import pytest

from js import network_display


def failure(message, *, status=503, code=None, provider=None, body=None):
    error = ai.ProviderAPIError(message, code=code, body=body)
    error.http_context = type("Context", (), {"status_code": status})()
    if provider:
        error.provider = provider
    return error


@pytest.mark.parametrize("error, service, diagnosis", [
    (failure('{"error":{"code":"server_is_overloaded"}}', status=502,
             provider="codex"), "OpenAI", "overloaded"),
    (failure("busy", status=529, provider="anthropic"), "Anthropic", "overloaded"),
    (failure("unavailable", status=503), "upstream service", "unavailable"),
    (failure("auth_unavailable: no auth available (providers=codex; last upstream error: server_is_overloaded)"),
     "proxy", "OpenAI"),
    (failure("auth_unavailable: no auth available"), "proxy", "account"),
    (failure("upstream stream closed before first payload", code="empty_stream"),
     "upstream service", "before sending"),
])
def test_outages_have_plain_diagnosis(error, service, diagnosis):
    text = str(network_display.describe_failure(error))
    assert service in text
    assert diagnosis in text
    assert "{" not in text
    assert "auth_unavailable" not in text
    assert "server_is_overloaded" not in text


def test_nested_outage_keeps_provider_evidence():
    outer = RuntimeError("request failed")
    outer.__cause__ = failure("busy", status=529, provider="anthropic")
    assert "Anthropic" in str(network_display.describe_failure(outer))


def test_status_alone_does_not_invent_upstream_identity():
    text = str(network_display.describe_failure(failure("unavailable")))
    assert "OpenAI" not in text
    assert "Anthropic" not in text


def test_non_outage_error_keeps_existing_detail():
    text = str(network_display.describe_failure(failure("quota exhausted", status=429)))
    assert "429" in text
    assert "quota exhausted" in text
