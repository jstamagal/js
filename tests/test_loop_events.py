"""Parts hand structured output to a caller's sink without writing to stdio."""

from types import SimpleNamespace

from js import output, stream_transport


def test_network_fallback_emits_an_event_before_rendering(capsys):
    events = []
    with output.using_sink(SimpleNamespace(emit=events.append)):
        stream_transport.say_or_print(2, "catalog")
    assert len(events) == 1
    assert events[0].name == "network"
    assert dict(events[0].fields) == {"level": 2, "line": "catalog", "fallback": True}
    assert capsys.readouterr() == ("", "")


def test_retry_emits_its_wait_and_failure_without_rendering(capsys):
    from js import retry

    events = []
    failure = RuntimeError("busy")
    with output.using_sink(SimpleNamespace(emit=events.append)):
        retry.announce(1, retry.Budget(attempts=2, max_wait=0), 12, failure)
    assert [event.name for event in events] == ["retry_wait"]
    fields = dict(events[0].fields)
    assert (fields["n"], fields["attempts"], fields["delay"], fields["error"]) == (1, 2, 12, failure)
    assert capsys.readouterr() == ("", "")
