"""Structured output: the one shape every js output event takes on its way to
a view (stdout today, irciipy + windows later).

See docs/nonblocking-windows.md. Nothing here is wired into the runtime yet —
this is step 0's data contract. A turn emits `OutputEvent`s instead of writing
to stdout; a `Sink` renders them. The default sink reproduces today's behavior
byte-for-byte so the flag can stay off with zero change.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Protocol


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
    `fields` carry named values for richer hooks and structured logs; `text` is
    the fallback rendering used only when no hook formats the event."""

    name: str
    source: str = ""                       # model!provider@baseurl; "" = the harness itself
    args: tuple[str, ...] = ()
    fields: dict[str, object] = field(default_factory=dict)
    text: str | None = None


class Sink(Protocol):
    """A view subscribes by being a Sink. The runtime only ever calls emit()."""

    def emit(self, event: OutputEvent) -> None: ...


class StdoutSink:
    """Default sink: print the event's `text` exactly as code prints today.

    Streaming text (name == "stream") is written without a trailing newline so a
    chunk stream reads identically to the current `_emit_text` path; everything
    else prints as a line. This is the byte-for-byte fallback for flag-off mode.
    """

    def emit(self, event: OutputEvent) -> None:
        if event.text is None:
            return
        if event.name == "stream":
            sys.stdout.write(event.text)
            sys.stdout.flush()
        else:
            print(event.text)
