"""What a turn shows and tells while it runs: its events, and the streamed reply.

`TurnEvents` raises a turn's events to its ON hooks and its event sink, and
tallies the usage the turn is charged.

`StreamSink` takes the chunks of a streamed reply. It sends them on as output
events, and keeps what streamed until the assistant record is appended, so a
turn cancelled mid-stream can still record it.
"""

from __future__ import annotations

from typing import Any

from . import memory
from . import output
from . import settings as _settings
from . import usage as usage_mod


class TurnEvents:
    def __init__(self, hooks: Any, sink: Any, telemetry: Any, *, model: str, provider_id: str | None):
        self._hooks = hooks
        self._sink = sink
        self._telemetry = telemetry
        self._model = model
        self._provider_id = provider_id
        self.usage = usage_mod.Tally()
        self.ended = False

    def emit(self, event: str, *, sink_extra: dict | None = None, **payload: Any) -> Any:
        """Raise ``event`` to the ON hooks and the event sink; the emission, or
        None when this turn has no hooks. ``sink_extra`` fields reach the sink only."""
        if self._sink is not None:
            try:
                self._sink(event, {**payload, **(sink_extra or {})})
            except Exception as exc:  # noqa: BLE001 - an observer never breaks the turn
                self._telemetry.event("event_sink_error", event=event, error=f"{type(exc).__name__}: {exc}")
        if self._hooks is None:
            return None
        emission = self._hooks.emit(event, **payload)
        for result in emission.results:
            if result.error:
                self._telemetry.event(
                    "event_handler_error",
                    event=emission.event,
                    handler=result.hook.handler,
                    error=result.error,
                )
        return emission

    def start(self, message_count: int) -> None:
        self.emit("turn_start", model=self._model, provider_id=self._provider_id, message_count=message_count)

    def end(self, reason: str, **extra: Any) -> None:
        """Raise turn_end; the event sink also gets the turn's usage totals."""
        self.ended = True
        self.emit("turn_end", reason=reason, model=self._model, provider_id=self._provider_id,
                  sink_extra={"usage": self.usage.as_dict()}, **extra)

    def on_usage(self, call: usage_mod.CallUsage, session: usage_mod.UsageTotals) -> None:
        """The usage meter's callback: tally ``call`` and tell the event sink."""
        self.usage.add(call)
        if self._sink is not None:
            self._sink("usage", {**call.as_dict(), "session": session.as_dict()})


def reasoning_level(settings: dict | None) -> int:
    """REASONING_DISPLAY, or its js/jsrc value when the store holds no level 0-3."""
    level = _settings.knob(settings, "REASONING_DISPLAY")
    if not isinstance(level, int) or level not in range(4):
        level = _settings.default_value("REASONING_DISPLAY")
    return level


class StreamSink:
    """``text`` and ``reasoning`` are the ``on_text`` and ``on_reasoning``
    callbacks of one model call. Each chunk goes to the turn status, and as
    an ``answer`` or ``reasoning`` output event to the telemetry's sink
    (`output.sink_of`), which draws the answer and the reasoning; ``close``
    ends both. The events say ``suppressed`` when ``suppress_output`` is set,
    and js's console then shows nothing; it shows no reasoning at
    REASONING_DISPLAY 0. The text also reaches the ``stream`` event."""

    def __init__(self, telemetry: Any, turn_status: Any, events: TurnEvents, *,
                 settings: dict | None, suppress_output: bool):
        self._telemetry = telemetry
        self._status = turn_status
        self._events = events
        self._suppress = suppress_output
        self._markdown = _settings.knob(settings if isinstance(settings, dict) else None, "MARKDOWN") is not False
        self._reasoning_level = reasoning_level(settings)
        self._key = output.Key()
        # Streamed and not yet recorded.
        self._text = ""
        self._reasoning: list[str] = []
        self._answer_open = False
        self._reasoning_open = False
        self._output("stream_open")

    def _output(self, name: str, /, **fields: Any) -> None:
        output.sink_of(self._telemetry).emit(output.event(name, stream=self._key, **fields))

    @property
    def shown(self) -> bool:
        """Reply text of this call is already on the screen or stdout."""
        return not self._suppress and bool(self._text)

    def clear(self) -> None:
        """Forget what streamed: at the start of each model call, and once
        the reply is in the history, so a later cancel does not append it again."""
        self._text = ""
        self._reasoning.clear()

    def reasoning(self, chunk: str) -> None:
        if not chunk:
            return
        self._reasoning.append(chunk)
        self._reasoning_open = True
        self._status.stream(chunk)
        self._output("reasoning", text=chunk, level=self._reasoning_level, suppressed=self._suppress)

    def text(self, chunk: str) -> None:
        if not chunk:
            return
        self._answer_open = True
        self._output("answer_started", suppressed=self._suppress)
        self._text += chunk
        self._status.stream(chunk)
        self._events.emit("stream", text=chunk)
        self._output("answer", text=chunk, markdown=self._markdown, suppressed=self._suppress)

    def close(self, reasoning_tokens: int | None = None) -> None:
        """Finish the answer and reasoning of this call."""
        if self._answer_open:
            self._answer_open = False
            self._output("answer_end", suppressed=self._suppress)
        self.close_reasoning(reasoning_tokens)

    def close_reasoning(self, tokens: int | None = None) -> None:
        if self._reasoning_open:
            self._reasoning_open = False
            self._output("reasoning_end", tokens=tokens)

    def commit_partial(self, messages: list[dict]) -> None:
        """Append what streamed and was not recorded, as an assistant record
        with incomplete_reason "cancelled". A partial record marks progress
        even when its reasoning was hidden, so the caller keeps the turn."""
        partial = self._text
        partial_reasoning = "".join(self._reasoning)
        self.clear()
        if not partial and not partial_reasoning:
            return
        record = {"role": "assistant", "content": partial, "incomplete_reason": "cancelled"}
        if partial_reasoning:
            record["reasoning_content"] = partial_reasoning
        messages.append(memory.note_time(record))
