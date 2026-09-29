"""One model call of a turn, driven without a turn: retries, overflow
recovery, the escalated resend and the resend without signed reasoning."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import ai
import ai.types.usage
import httpx
import pytest

from js import compaction, model_client, retry, turn_call
from js.model_client import ModelStreamResult, ModelToolCall
from js.toolkit import ToolContext
from js.toolkit.core import TurnStatus
from js.turn_budget import TurnConvo
from js.turn_call import CallLimits, ModelCaller, ModelRequest
from js.turn_stream import StreamSink, TurnEvents


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(turn_call, "_backoff", lambda _n: 0)
    monkeypatch.setattr(turn_call.model_metadata, "resolve_max_output", lambda *a, **k: None)


def _done(text: str = "done", *, input_tokens: int = 10, output_tokens: int = 5) -> ModelStreamResult:
    return ModelStreamResult(
        text=text, tool_calls=[], reasoning="",
        usage=ai.types.usage.Usage(input_tokens=input_tokens, output_tokens=output_tokens),
        finish_reason="stop", assistant_message=ai.assistant_message(text),
    )


def _cut(text: str, calls: list[ModelToolCall] | None = None) -> ModelStreamResult:
    return ModelStreamResult(
        text=text, tool_calls=calls or [], reasoning="",
        usage=ai.types.usage.Usage(input_tokens=10, output_tokens=1000),
        finish_reason="length", assistant_message=ai.assistant_message(text),
        incomplete_reason="max_output_tokens",
    )


def _retryable(headers: dict[str, str] | None = None) -> ai.ProviderAPIError:
    response = httpx.Response(429, headers=headers or {})
    return ai.ProviderRateLimitError(
        "slow down", http_context=ai.errors.HTTPErrorContext(status_code=429, response=response))


def _overflow() -> ai.ProviderAPIError:
    return ai.ProviderAPIError(
        "request (97720 tokens) exceeds the available context size (89600 tokens)",
        provider="openai", error_type="exceed_context_size_error",
    )


def _fatal() -> ai.ProviderAPIError:
    return ai.ProviderStatusError("HTTP 401", provider="openai", code="401", is_retryable=False)


def _signature_refused() -> ai.ProviderAPIError:
    return ai.ProviderBadRequestError("messages.1.content.0: Invalid `signature` in `thinking` block",
                                      provider="anthropic")


class _Budget:
    """Records the budget calls; recovery changes the history when ``recovers``."""

    def __init__(self, *, window=None, recovers=True):
        self.window = window
        self.recovers = recovers
        self.fits: list[str] = []
        self.recoveries: list[tuple[type, int]] = []

    async def fit(self, *, phase, specs, force=False, overflow_round=0):
        self.fits.append(phase)
        return False

    def provider_window(self):
        return self.window

    async def recover_overflow(self, error, *, overflow_round, tools, specs):
        self.recoveries.append((type(error), overflow_round))
        return self.recovers


class _Turn:
    """A ModelCaller over scripted replies, and what it did."""

    def __init__(self, tmp_path, monkeypatch, replies, *, budget=None, attempts=2, max_wait=0.0,
                 escalation=64000, max_out=1000, provider_id="openai",
                 messages=None):
        self.calls: list[dict] = []

        def stream(**kwargs):
            self.calls.append(kwargs)
            reply = replies[min(len(self.calls), len(replies)) - 1]
            if isinstance(reply, BaseException):
                raise reply
            return reply

        monkeypatch.setattr(model_client, "stream_model_async", stream)
        self.events: list[tuple[str, dict]] = []
        self.telemetry = SimpleNamespace(
            event=lambda kind, **fields: self.events.append((kind, fields)),
            trace_sink=None, transcript_log=None, reasoning_factory=None, display_factory=None,
        )
        self.raised: list[str] = []
        events = TurnEvents(None, lambda event, payload: self.raised.append(event), self.telemetry,
                            model="m", provider_id=provider_id)
        self.messages = messages if messages is not None else [{"role": "user", "content": "go"}]
        self.convo = TurnConvo("SYSTEM", self.messages, provider_id=provider_id, model="m")
        self.context = ToolContext(cwd=tmp_path)
        self.budget = budget or _Budget()
        status = TurnStatus()
        self.caller = ModelCaller(
            ModelRequest(model="m", provider_id=provider_id, base_url=None, api_key=None,
                         effort=None, max_out=max_out),
            CallLimits(retry=retry.Budget(attempts=attempts, max_wait=max_wait), stream_idle=None,
                       max_output_escalation=escalation, max_output_resumes=0),
            convo=self.convo, budget=self.budget,
            sink=StreamSink(self.telemetry, status, events, settings={}, suppress_output=True),
            events=events, telemetry=self.telemetry, context=self.context, turn_status=status,
            registry=SimpleNamespace(openai_specs=lambda: []), alias=lambda specs: specs,
        )

    def call(self, phase="preflight"):
        return asyncio.run(self.caller.call(phase=phase))

    def kinds(self):
        return [kind for kind, _ in self.events]


def test_a_reply_comes_back_with_what_the_turn_records(tmp_path, monkeypatch):
    turn = _Turn(tmp_path, monkeypatch, [_done("hello")])
    reply = turn.call(phase="midturn")
    assert reply.result.text == "hello"
    assert (reply.finish, reply.incomplete_reason, reply.usage_stale) == ("stop", None, False)
    assert turn.budget.fits == ["midturn"]
    assert turn.raised == ["prompt"]
    assert turn.context.last_prompt_tokens == 10
    assert turn.context.last_output_tokens == 5


def test_retryable_errors_are_retried_within_the_budget(tmp_path, monkeypatch):
    turn = _Turn(tmp_path, monkeypatch, [_retryable(), _retryable(), _done()], attempts=2)
    assert turn.call().result.text == "done"
    assert len(turn.calls) == 3
    assert turn.kinds().count("retriable_error") == 2
    assert turn.budget.fits == ["preflight"]


def test_a_retryable_error_past_the_budget_ends_the_turn(tmp_path, monkeypatch):
    error = _retryable()
    turn = _Turn(tmp_path, monkeypatch, [error], attempts=2)
    with pytest.raises(ai.ProviderAPIError) as caught:
        turn.call()
    assert caught.value is error
    assert len(turn.calls) == 3
    assert turn.raised[-2:] == ["error", "turn_end"]


def test_a_retry_after_longer_than_the_limit_fails_at_once(tmp_path, monkeypatch):
    turn = _Turn(tmp_path, monkeypatch, [_retryable({"retry-after": "30"})], attempts=5, max_wait=10)
    with pytest.raises(ai.ProviderAPIError):
        turn.call()
    assert len(turn.calls) == 1
    assert "retry_after_too_long" in turn.kinds()


@pytest.mark.parametrize("error", [
    _fatal(),
    ai.ConfigurationError("no key"),
    ValueError("bad request shape"),
])
def test_a_fatal_error_ends_the_turn_without_a_retry(tmp_path, monkeypatch, error):
    turn = _Turn(tmp_path, monkeypatch, [error])
    with pytest.raises(type(error)):
        turn.call()
    assert len(turn.calls) == 1
    assert "fatal_error" in turn.kinds()
    assert turn.raised[-2:] == ["error", "turn_end"]


def test_an_overflow_is_recovered_and_the_request_sent_again(tmp_path, monkeypatch):
    turn = _Turn(tmp_path, monkeypatch, [_overflow(), _overflow(), _done()])
    assert turn.call().result.text == "done"
    assert turn.budget.recoveries == [(ai.ProviderAPIError, 1), (ai.ProviderAPIError, 2)]
    assert len(turn.calls) == 3


def test_overflow_rounds_are_counted_across_the_turns_calls(tmp_path, monkeypatch):
    replies = [_overflow()] * compaction.MAX_OVERFLOW_ROUNDS + [_done(), _overflow()]
    turn = _Turn(tmp_path, monkeypatch, replies)
    turn.call()
    with pytest.raises(ai.ProviderAPIError):
        turn.call()
    assert len(turn.budget.recoveries) == compaction.MAX_OVERFLOW_ROUNDS


def test_an_overflow_nothing_can_shed_ends_the_turn(tmp_path, monkeypatch):
    turn = _Turn(tmp_path, monkeypatch, [_overflow(), _done()], budget=_Budget(recovers=False))
    with pytest.raises(ai.ProviderAPIError):
        turn.call()
    assert len(turn.calls) == 1


def test_a_silent_overflow_is_shed_and_asked_again(tmp_path, monkeypatch):
    turn = _Turn(tmp_path, monkeypatch, [_done("cut input", input_tokens=5000), _done("whole")],
                 budget=_Budget(window=4000))
    assert turn.call().result.text == "whole"
    assert turn.budget.recoveries == [(compaction.SilentOverflowError, 1)]
    assert "context_overflow_silent" in turn.kinds()


def test_a_cut_off_reply_is_resent_once_with_a_larger_cap(tmp_path, monkeypatch):
    turn = _Turn(tmp_path, monkeypatch, [_cut("half"), _cut("still half")], escalation=8000)
    reply = turn.call()
    assert [c["max_output_tokens"] for c in turn.calls] == [1000, 8000]
    assert reply.result.text == "still half"
    assert reply.incomplete_reason == "max_output_tokens"
    turn.call()
    assert [c["max_output_tokens"] for c in turn.calls] == [1000, 8000, 1000]


def test_a_refused_escalated_cap_falls_back_to_the_configured_cap(tmp_path, monkeypatch):
    turn = _Turn(tmp_path, monkeypatch, [_cut("half"), _fatal(), _cut("half again")], escalation=8000)
    assert turn.call().result.text == "half again"
    assert [c["max_output_tokens"] for c in turn.calls] == [1000, 8000, 1000]
    assert "max_output_escalation_rejected" in turn.kinds()


def test_a_cut_off_tool_call_with_whole_arguments_is_not_resent(tmp_path, monkeypatch):
    call = ModelToolCall(id="c1", name="read", arguments='{"path": "x"}')
    turn = _Turn(tmp_path, monkeypatch, [_cut("", [call]), _done()], escalation=8000)
    turn.call()
    assert len(turn.calls) == 1


def test_a_refused_signature_is_dropped_once_and_the_history_replayed(tmp_path, monkeypatch):
    messages = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "ok", "reasoning_content": "thought",
         "reasoning_parts": [{"type": "thinking", "text": "thought", "signature": "sig"}],
         "reasoning_from": "anthropic:m"},
        {"role": "user", "content": "again"},
    ]
    turn = _Turn(tmp_path, monkeypatch, [_signature_refused(), _signature_refused()],
                 provider_id="anthropic", messages=messages)
    first = turn.convo.ai
    with pytest.raises(ai.ProviderAPIError):
        turn.call()
    assert len(turn.calls) == 2
    assert turn.kinds().count("signed_reasoning_dropped") == 1
    assert "reasoning_parts" not in messages[1]
    assert turn.convo.ai is not first


def test_call_limits_read_the_resilience_settings():
    limits = CallLimits.from_settings({"runtime": {
        "retry_attempts": 4, "retry_max_wait_seconds": 60, "stream_idle_seconds": 0,
        "max_output_escalation": 32000, "max_output_resumes": 2}})
    assert limits == CallLimits(retry=retry.Budget(attempts=4, max_wait=60.0), stream_idle=None,
                                max_output_escalation=32000, max_output_resumes=2)
