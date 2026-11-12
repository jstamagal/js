import io
import json

import pytest

from js.headless import JsonEvents


@pytest.mark.parametrize("payload", [
    {"kind": "payload-kind", "input_tokens": 3},
    {"type": "payload-type", "input_tokens": 3},
    {"kind": "payload-kind", "type": "payload-type", "self": "payload-self",
     "fields": {"extra": True}, "input_tokens": 3},
])
def test_runtime_usage_payload_preserves_event_discriminator(payload):
    out = io.StringIO()
    events = JsonEvents(out)
    events.runtime_event("usage", payload)
    value = json.loads(out.getvalue())
    assert value == {**payload, "type": "usage"}
    assert not events.errored
    assert events.session is None


def test_emit_payload_cannot_override_event_state():
    out = io.StringIO()
    events = JsonEvents(out)
    events.emit("session", kind="payload", type="error", id="session-id")
    assert not events.errored
    assert events.session == {"kind": "payload", "type": "error", "id": "session-id"}
    events.emit("error", kind="payload", type="session", message="failure")
    assert events.errored
    assert events.session["id"] == "session-id"
    values = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [value["type"] for value in values] == ["session", "error"]


def test_unknown_runtime_event_does_not_break_later_stream_events():
    out = io.StringIO()
    events = JsonEvents(out)
    events.runtime_event("unknown-extra", {"kind": "payload", "type": "error"})
    events.runtime_event("usage", {"kind": "extra", "output_tokens": 2})
    events.runtime_event("response", {"text": "done", "finish_reason": "stop"})
    assert [json.loads(line)["type"] for line in out.getvalue().splitlines()] == ["usage", "message"]
    assert events.final_text == "done"
