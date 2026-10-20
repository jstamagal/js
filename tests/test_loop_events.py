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


def test_compaction_flight_failure_goes_to_the_callback(monkeypatch, capsys):
    import pytest

    from js import compaction

    failure = OSError("read only")
    def unavailable(*args, **kwargs):
        raise failure

    monkeypatch.setattr(compaction, "CompactionFlight", unavailable)
    events = []
    with pytest.raises(OSError):
        compaction._clearing_flight([], cfg=None, system="", trigger={}, flight_data={},
                                    log_dir=None, emit=lambda name, **fields: events.append((name, fields)))
    assert events[0][0] == "compaction_output"
    notice = events[0][1]["output"]
    assert dict(notice.fields)["fields"]["error"] is failure
    assert capsys.readouterr() == ("", "")
