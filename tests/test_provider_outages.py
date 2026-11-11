"""Outage diagnostics name only services identified by the failure."""

import asyncio
from types import SimpleNamespace

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


def test_proxy_upstream_outranks_transport_provider():
    error = failure("auth_unavailable: providers=anthropic; overloaded_error", provider="openai")
    text = str(network_display.describe_failure(error))
    assert "Anthropic" in text
    assert "OpenAI" not in text


def test_non_outage_error_keeps_existing_detail():
    text = str(network_display.describe_failure(failure("quota exhausted", status=429)))
    assert "429" in text
    assert "quota exhausted" in text


def test_retries_collapse_but_keep_final_action(monkeypatch):
    from js.console import ConsoleSink

    lines = []
    monkeypatch.setattr(network_display, "always", lambda line: lines.append(str(line)))
    sink = ConsoleSink(SimpleNamespace())
    error = failure("busy", status=529, provider="anthropic")
    for n in range(1, 5):
        sink._on_retry_wait(n=n, attempts=4, delay=8, error=error, label="")
    sink._on_retry_gave_up(n=4, error=error, label="")
    assert len(lines) == 2
    assert "Retrying" in lines[0]
    assert "repeated 5 times" in lines[-1]
    assert "4 retries" in lines[-1]
    assert "try again" in lines[-1]


def test_retry_sequences_are_task_local_and_reset(monkeypatch):
    from js.console import ConsoleSink

    lines = []
    monkeypatch.setattr(network_display, "always", lambda line: lines.append(str(line)))
    sink = ConsoleSink(SimpleNamespace())
    error = failure("busy", status=529, provider="anthropic")

    async def sequence():
        for n in (1, 2):
            sink._on_retry_wait(n=n, attempts=2, delay=0, error=error, label="")
            await asyncio.sleep(0)
        sink._on_retry_gave_up(n=2, error=error, label="")

    async def run():
        await asyncio.gather(sequence(), sequence())
        await sequence()

    asyncio.run(run())
    assert len(lines) == 6
    assert sum("Retrying" in line for line in lines) == 3


def test_raw_errors_stay_in_debug_log_even_when_screen_collapses(monkeypatch, tmp_path):
    import json

    from js.console import ConsoleSink
    from js.runtime import Telemetry

    lines = []
    monkeypatch.setattr(network_display, "always", lambda line: lines.append(str(line)))
    log = tmp_path / "debug.jsonl"
    sink = ConsoleSink(Telemetry(debug_log=log))
    raw = '{"error":{"code":"server_is_overloaded","message":"busy"}}'
    error = failure(raw, status=502, provider="codex")
    for n in (1, 2):
        sink._on_retry_wait(n=n, attempts=2, delay=0, error=error, label="")
    sink._on_retry_gave_up(n=2, error=error, label="")
    records = [json.loads(line) for line in log.read_text().splitlines()]
    assert [record["error"] for record in records] == [raw, raw, raw]
    assert all(raw not in line for line in lines)
    assert len(lines) == 2


def test_final_cli_error_has_no_raw_body():
    from js.cli import _error_text

    text = str(_error_text(failure('{"error":{"code":"server_is_overloaded"}}',
                                  status=502, provider="codex")))
    assert "OpenAI" in text
    assert "Try again" in text
    assert "{" not in text


def test_success_reports_repeat_count_and_clears_state(monkeypatch):
    from js.console import ConsoleSink

    lines = []
    monkeypatch.setattr(network_display, "always", lambda line: lines.append(str(line)))
    sink = ConsoleSink(SimpleNamespace())
    error = failure("busy", status=529)
    for n in (1, 2, 3):
        sink._on_retry_wait(n=n, attempts=3, delay=0, error=error, label="")
    sink._on_call_stats(label="", elapsed_s=1, finish="stop", tool_calls=0,
                        tokens=1, first_token_s=0, prompt_tokens=1, cached_tokens=0,
                        trace=False)
    assert len(lines) == 2
    assert "3" in lines[-1]
    assert sink._retry_repeat.get() is None
