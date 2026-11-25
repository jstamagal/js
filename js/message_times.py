"""When each message happened: noted as it happens and taken when its record is written, so a session's records carry the time of the event, not of the write."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict

# When each live message happened, by message object. A turn's messages are
# persisted when the turn ends; the record written for a noted message carries
# the time noted, not the write time. An entry leaves when its record is
# written, or when more than _EVENT_TIMES_MAX are held.
_EVENT_TIMES: OrderedDict[int, tuple[dict, float]] = OrderedDict()
_EVENT_TIMES_MAX = 4096
_event_times_lock = threading.Lock()


def note_time(message: dict, ts: float | None = None) -> dict:
    """Remember when ``message`` happened (now when ``ts`` is None); the record
    later written for it carries that time. Returns ``message``."""
    with _event_times_lock:
        _EVENT_TIMES[id(message)] = (message, time.time() if ts is None else ts)
        _EVENT_TIMES.move_to_end(id(message))
        while len(_EVENT_TIMES) > _EVENT_TIMES_MAX:
            _EVENT_TIMES.popitem(last=False)
    return message


def take_time(message: dict) -> float:
    """The noted time of ``message``, else now. The note is dropped."""
    with _event_times_lock:
        entry = _EVENT_TIMES.get(id(message))
        if entry is None or entry[0] is not message:
            return time.time()
        del _EVENT_TIMES[id(message)]
        return entry[1]
