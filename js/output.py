"""Structured output: the one shape every js output event takes on its way to
a view.

A turn emits `OutputEvent`s instead of writing to stdout; a `Sink` renders
them. `Telemetry.sink` is the sink a turn emits to; without one, `sink_of`
gives js's own, the console (`js/console.py`). See docs/nonblocking-windows.md.

The loop's events, and their fields:

    tool_call      call, name, args, malformed, context, traced, deferred
    tool_result    call, name, args, result, malformed, context, traced
    tools_settle   (none): the run of quiet tool calls ends
    run_line       fields: the run line's "key=value" strings
    call_stats     label, elapsed_s, finish, tool_calls, tokens,
                   first_token_s, prompt_tokens, cached_tokens
    stream_open    stream: a turn's streamed reply begins
    answer_started stream, suppressed
    answer         stream, text, markdown, suppressed
    answer_end     stream, suppressed
    reasoning      stream, text, level, suppressed
    reasoning_end  stream, tokens
    notice         message, fields, stderr, flush
    network        level, line (a Message's Said or text), fallback

``call`` ties a tool_result to its tool_call; ``stream`` ties the chunks of
one turn's streamed reply together; ``context`` is the call's ToolContext.
``traced`` is the trace setting for an exchange; ``deferred`` asks the console
to draw the exchange whole when it finishes. Every call still reaches the sink.

"""

from __future__ import annotations

import sys
import threading
from dataclasses import dataclass, field
from typing import Any, Protocol

# Serializes output from the threads of one dispatch batch.
LOCK = threading.RLock()


def agent_identity(model: str, provider: str | None, base_url: str | None) -> str:
    """IRC-style `nick!user@host` for an agent: `model!provider@baseurl`.

    This is the `source` on every event the agent emits, so a window can bind
    to it by glob (`opus*!anthropic@*`). Subagent N is just another identity.
    """
    nick = model or "?"
    user = provider or "ai-sdk"
    host = (base_url or "default").removeprefix("https://").removeprefix("http://").rstrip("/")
    return f"{nick}!{user}@{host}"


@dataclass(frozen=True)
class OutputEvent:
    """One unit of output. `args` map to irciipy positionals ($0 $1 ...);
    `fields` carry named values for richer hooks and structured logs, as a tuple
    of (key, value) pairs; build a lookup with `dict(event.fields)` when needed.
    Fields participate in equality and are excluded from the hash, since their
    structured payloads can contain dictionaries and lists.
    `text` is the fallback rendering used only when no hook formats the event."""

    name: str
    source: str = ""  # model!provider@baseurl; "" = the harness itself
    args: tuple[str, ...] = ()
    fields: tuple[tuple[str, object], ...] = field(default=(), hash=False)
    text: str | None = None


class Sink(Protocol):
    """A view subscribes by being a Sink. The runtime only ever calls emit()."""

    def emit(self, event: OutputEvent) -> None: ...


def event(name: str, /, **fields: Any) -> OutputEvent:
    """An `OutputEvent` named ``name`` carrying ``fields``."""
    return OutputEvent(name, fields=tuple(fields.items()))


class Key:
    """What events that belong together share: a tool_call and its
    tool_result, the events of one turn's streamed reply."""

    __slots__ = ("__weakref__",)


def sink_of(telemetry: Any) -> Sink:
    """The sink ``telemetry`` emits to: its ``sink``, else js's own console,
    made once per telemetry."""
    sink = getattr(telemetry, "sink", None)
    if sink is not None:
        return sink
    console = getattr(telemetry, "console", None)
    if console is not None:
        return console
    from .console import ConsoleSink

    with LOCK:
        console = getattr(telemetry, "console", None)
        if console is None:
            console = ConsoleSink(telemetry)
            try:
                telemetry.console = console
            except AttributeError:
                pass
    return console


class StdoutSink:
    """Default sink: print the event's `text` exactly as code prints today.

    Streaming text (name == "stream") is written without a trailing newline so a
    chunk stream reads identically to `StreamSink.text` (js/turn_stream.py); everything
    else prints as a line. This is the byte-for-byte fallback for flag-off mode.
    """

    def __init__(self, transcript_log: object | None = None) -> None:
        self.transcript_log = transcript_log

    def emit(self, event: OutputEvent) -> None:
        if event.text is None:
            return
        if event.name == "stream":
            if self.transcript_log is not None:
                write_chunk = getattr(self.transcript_log, "write_assistant_chunk", None)
                if callable(write_chunk):
                    write_chunk(event.text)
                mute = getattr(self.transcript_log, "mute_tee", None)
            else:
                mute = None
            ctx = mute() if callable(mute) else _NullContext()
            with ctx:
                sys.stdout.write(event.text)
                sys.stdout.flush()
        else:
            print(event.text)


class _NullContext:
    def __enter__(self):
        return None

    def __exit__(self, *_args):
        return False
