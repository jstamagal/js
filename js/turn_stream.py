"""What a turn shows and tells while it runs: its events, and the streamed reply.

`TurnEvents` raises a turn's events to its ON hooks and its event sink, and
tallies the usage the turn is charged.

`StreamSink` takes the chunks of a streamed reply. It shows the answer in a
Display and the reasoning in a ReasoningDisplay, tees the answer to the
transcript log, and keeps what streamed until the assistant record is
appended, so a turn cancelled mid-stream can still record it.
"""

from __future__ import annotations

import contextlib
import sys
from typing import Any

from . import display
from . import memory
from . import settings as _settings
from . import usage as usage_mod
from .reasoning_display import ReasoningDisplay, StderrReasoning


class TurnEvents:
    def __init__(self, hooks: Any, sink: Any, telemetry: Any, *, model: str, provider_id: str | None):
        self._hooks = hooks
        self._sink = sink
        self._telemetry = telemetry
        self._model = model
        self._provider_id = provider_id
        self.usage = usage_mod.Tally()

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
        self.emit("turn_end", reason=reason, model=self._model, provider_id=self._provider_id,
                  sink_extra={"usage": self.usage.as_dict()}, **extra)

    def on_usage(self, call: usage_mod.CallUsage, session: usage_mod.UsageTotals) -> None:
        """The usage meter's callback: tally ``call`` and tell the event sink."""
        self.usage.add(call)
        if self._sink is not None:
            self._sink("usage", {**call.as_dict(), "session": session.as_dict()})


def reasoning_level(settings: dict | None) -> int:
    """ui.reasoning, or its js/jsrc value when the store holds no level 0-3."""
    level = _settings.knob(settings, "ui.reasoning")
    if not isinstance(level, int) or level not in range(4):
        level = _settings.default_value("ui.reasoning")
    return level


class StreamSink:
    """``text`` and ``reasoning`` are the ``on_text`` and ``on_reasoning``
    callbacks of one model call. Each Display opens at its first chunk and
    finishes at ``close``. Nothing is shown when ``suppress_output`` is set,
    and no reasoning at ui.reasoning 0; the text still reaches the turn status
    and the ``stream`` event."""

    def __init__(self, telemetry: Any, turn_status: Any, events: TurnEvents, *,
                 settings: dict | None, suppress_output: bool):
        self._telemetry = telemetry
        self._status = turn_status
        self._events = events
        self._suppress = suppress_output
        self._markdown = display.markdown_enabled(settings)
        self._reasoning_level = reasoning_level(settings)
        self._transcript = getattr(telemetry, "transcript_log", None)
        self._answer: display.Display | None = None
        self._thinking: ReasoningDisplay | None = None
        # Streamed and not yet recorded.
        self._text = ""
        self._reasoning: list[str] = []

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
        self._status.stream(chunk)
        if self._suppress or self._reasoning_level == 0:
            return
        if self._thinking is None:
            factory = self._telemetry.reasoning_factory
            self._thinking = (
                factory(self._reasoning_level) if factory is not None
                else StderrReasoning(self._reasoning_level, sys.stderr)
            )
        self._thinking.append(chunk)

    def text(self, chunk: str) -> None:
        if not chunk:
            return
        if self._thinking is not None:
            self._thinking.answer_started()
        self._text += chunk
        self._status.stream(chunk)
        self._events.emit("stream", text=chunk)
        if self._suppress:
            return
        if self._transcript is not None:
            write_chunk = getattr(self._transcript, "write_assistant_chunk", None)
            if callable(write_chunk):
                write_chunk(chunk)
        with self._muted_transcript_tee():
            if self._answer is None:
                factory = self._telemetry.display_factory
                self._answer = (
                    factory(self._markdown) if factory is not None
                    else display.Display.for_stream(sys.stdout, markdown=self._markdown)
                )
                self._answer.mark(display.ASSISTANT_MARK)
            self._answer.chunk("text", chunk)

    def close(self, reasoning_tokens: int | None = None) -> None:
        """Finish the answer and reasoning Displays of this call."""
        if not self._suppress and self._answer is not None:
            if self._transcript is not None:
                end_stream = getattr(self._transcript, "end_assistant_stream", None)
                if callable(end_stream):
                    end_stream()
            with self._muted_transcript_tee():
                self._answer.finish()
        self._answer = None
        self.close_reasoning(reasoning_tokens)

    def close_reasoning(self, tokens: int | None = None) -> None:
        if self._thinking is not None:
            self._thinking.finish(tokens)
            self._thinking = None

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

    def _muted_transcript_tee(self):
        mute = getattr(self._transcript, "mute_tee", None)
        if callable(mute):
            return mute()
        return contextlib.nullcontext()
