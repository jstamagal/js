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
