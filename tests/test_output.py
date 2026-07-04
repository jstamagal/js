from __future__ import annotations

from js.output import OutputEvent, StdoutSink


def test_output_event_is_genuinely_hashable():
    """frozen=True advertises hashability; a dict `fields` field used to make
    hash() raise despite that. `fields` is now a tuple of (key, value) pairs,
    so hash() must work, e.g. for a view that dedups events in a set."""
    event = OutputEvent(name="turn_start", fields=(("agent_id", "worker-1"),))

    assert hash(event) == hash(event)
    assert event in {event}


def test_output_event_fields_round_trip_through_dict():
    event = OutputEvent(name="tool_call", fields=(("name", "read"), ("ok", True)))

    assert dict(event.fields) == {"name": "read", "ok": True}


def test_stdout_sink_prints_text_as_a_line(capsys):
    StdoutSink().emit(OutputEvent(name="turn_end", text="done"))

    assert capsys.readouterr().out == "done\n"


def test_stdout_sink_streams_text_without_trailing_newline(capsys):
    StdoutSink().emit(OutputEvent(name="stream", text="chunk"))

    assert capsys.readouterr().out == "chunk"
