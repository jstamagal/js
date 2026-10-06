"""A turn's events and its streamed reply, driven without a model."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from js import usage as usage_mod
from js.toolkit.core import TurnStatus
from js.turn_stream import StreamSink, TurnEvents


class _Display:
    def __init__(self, log, markdown):
        self.log = log
        self.log.append(("open", markdown))

    def mark(self, text):
        pass

    def chunk(self, kind, text):
        self.log.append((kind, text))

    def finish(self):
        self.log.append(("finish",))


class _Reasoning:
    def __init__(self, log, level):
        self.log = log
        self.log.append(("think", level))

    def append(self, text):
        self.log.append(("reason", text))

    def answer_started(self):
        self.log.append(("answer_started",))

    def finish(self, tokens=None):
        self.log.append(("reason_finish", tokens))


class _Transcript:
    def __init__(self, log):
        self.log = log

    def write_assistant_chunk(self, text):
        self.log.append(("transcript", text))

    def end_assistant_stream(self):
        self.log.append(("transcript_end",))


def _telemetry(log, *, transcript=False):
    return SimpleNamespace(
        event=lambda kind, **fields: log.append(("telemetry", kind)),
        display_factory=lambda markdown: _Display(log, markdown),
        reasoning_factory=lambda level: _Reasoning(log, level),
        transcript_log=_Transcript(log) if transcript else None,
    )


def _sink(log, *, suppress=False, reasoning=2, sink=None, transcript=False):
    telemetry = _telemetry(log, transcript=transcript)
    events = TurnEvents(None, sink, telemetry, model="m", provider_id="p")
    return StreamSink(telemetry, TurnStatus(), events,
                      settings={"ui": {"reasoning": reasoning, "markdown": False}},
                      suppress_output=suppress)


# --------------------------------------------------------------------------
# StreamSink
# --------------------------------------------------------------------------

def test_reasoning_then_answer_open_one_display_each_and_close_together():
    log = []
    sink = _sink(log, transcript=True)
    sink.reasoning("thinking")
    sink.text("hel")
    sink.text("lo")
    sink.close(reasoning_tokens=7)
    assert log == [
        ("think", 2), ("reason", "thinking"), ("answer_started",),
        ("transcript", "hel"), ("open", False), ("text", "hel"),
        ("answer_started",), ("transcript", "lo"), ("text", "lo"),
        ("transcript_end",), ("finish",), ("reason_finish", 7),
    ]
    assert sink.shown


def test_suppressed_output_shows_nothing_but_still_streams_the_event():
    log, seen = [], []
    sink = _sink(log, suppress=True, sink=lambda event, payload: seen.append((event, payload)))
    sink.reasoning("thinking")
    sink.text("answer")
    sink.close()
    assert log == []
    assert seen == [("stream", {"text": "answer"})]
    assert not sink.shown


def test_reasoning_level_zero_hides_the_reasoning_only():
    log = []
    sink = _sink(log, reasoning=0)
    sink.reasoning("thinking")
    sink.text("answer")
    assert [entry[0] for entry in log] == ["open", "text"]


def test_empty_chunks_are_ignored():
    log = []
    sink = _sink(log)
    sink.reasoning("")
    sink.text("")
    assert log == []
    assert not sink.shown


def test_a_cancel_records_what_streamed_as_a_partial_reply():
    messages = []
    sink = _sink([])
    sink.reasoning("because")
    sink.text("half an ans")
    sink.commit_partial(messages)
    assert len(messages) == 1
    record = messages[0]
    assert (record["role"], record["content"], record["incomplete_reason"], record["reasoning_content"]) == (
        "assistant", "half an ans", "cancelled", "because")
    sink.commit_partial(messages)
    assert len(messages) == 1


def test_a_cleared_sink_records_nothing_on_cancel():
    messages = []
    sink = _sink([])
    sink.text("already recorded")
    sink.clear()
    sink.commit_partial(messages)
    assert messages == []
    assert not sink.shown


def test_a_new_call_opens_new_displays():
    log = []
    sink = _sink(log)
    sink.text("one")
    sink.close()
    sink.text("two")
    assert log.count(("open", False)) == 2


# --------------------------------------------------------------------------
# TurnEvents
# --------------------------------------------------------------------------

def test_turn_end_gives_the_sink_the_turn_usage():
    seen = []
    events = TurnEvents(None, lambda event, payload: seen.append((event, payload)),
                        _telemetry([]), model="m", provider_id="p")
    call = usage_mod.CallUsage(model="m", provider="p", input_tokens=10, output_tokens=2)
    events.on_usage(call, usage_mod.UsageTotals())
    events.start(3)
    events.end("stop")
    assert [event for event, _ in seen] == ["usage", "turn_start", "turn_end"]
    assert seen[1][1] == {"model": "m", "provider_id": "p", "message_count": 3}
    end = seen[2][1]
    assert end["reason"] == "stop"
    assert end["usage"] == events.usage.as_dict()
    assert events.usage.as_dict() != usage_mod.Tally().as_dict()


def test_a_failing_event_sink_never_breaks_the_turn():
    log = []

    def broken(event, payload):
        raise RuntimeError("boom")

    events = TurnEvents(None, broken, _telemetry(log), model="m", provider_id="p")
    assert events.emit("stream", text="x") is None
    assert log == [("telemetry", "event_sink_error")]


def test_hooks_see_the_payload_without_the_sink_extras():
    received = []

    class _Hooks:
        def emit(self, event, **payload):
            received.append((event, payload))
            return SimpleNamespace(event=event, results=[])

    events = TurnEvents(_Hooks(), None, _telemetry([]), model="m", provider_id="p")
    events.end("stop")
    assert received == [("turn_end", {"reason": "stop", "model": "m", "provider_id": "p"})]


@pytest.mark.parametrize("error", [None, "handler failed"])
def test_a_handler_error_is_logged(error):
    log = []

    class _Hooks:
        def emit(self, event, **payload):
            result = SimpleNamespace(error=error, hook=SimpleNamespace(handler="h"))
            return SimpleNamespace(event=event, results=[result])

    events = TurnEvents(_Hooks(), None, _telemetry(log), model="m", provider_id="p")
    events.emit("stream", text="x")
    assert log == ([("telemetry", "event_handler_error")] if error else [])
