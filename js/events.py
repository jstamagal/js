"""Typed event names and hook registration for the ircII-style command layer."""

from __future__ import annotations

from dataclasses import dataclass


CANONICAL_EVENT_NAMES: tuple[str, ...] = (
    "input",
    "prompt",
    "stream",
    "tool_call",
    "tool_result",
    "response",
    "notice",
    "turn_start",
    "turn_end",
    "subagent",
    "error",
    "cancel",
    "idle",
)

_EVENT_SET = frozenset(CANONICAL_EVENT_NAMES)


@dataclass(frozen=True)
class EventHook:
    event: str
    handler: str
    suppress: bool = False


@dataclass(frozen=True)
class EventEmission:
    event: str
    payload: dict
    hooks: list[EventHook]


def normalize_event_name(raw: str) -> str | None:
    name = raw.strip().lower().replace("-", "_")
    return name if name in _EVENT_SET else None


def parse_event_token(raw: str) -> tuple[str | None, bool]:
    token = raw.strip()
    suppress = token.startswith("^")
    if suppress:
        token = token[1:]
    return normalize_event_name(token), suppress


class EventHooks:
    """In-memory ON hook table.

    The handler is kept as raw script text for now. The runtime can later decide
    how to execute it, while the command layer already has typed event names and
    a stable place for loaded scripts to register hooks.
    """

    def __init__(self) -> None:
        self._hooks: dict[str, list[EventHook]] = {event: [] for event in CANONICAL_EVENT_NAMES}

    def add(self, event_token: str, handler: str) -> EventHook:
        event, suppress = parse_event_token(event_token)
        if event is None:
            clean = event_token.strip().lstrip("^").lower().replace("-", "_")
            raise ValueError(f"unknown event: {clean}")
        text = handler.strip()
        if not text:
            raise ValueError("on needs a handler")
        hook = EventHook(event=event, handler=text, suppress=suppress)
        self._hooks[event].append(hook)
        return hook

    def handlers_for(self, event: str) -> list[EventHook]:
        name = normalize_event_name(event)
        if name is None:
            return []
        return list(self._hooks.get(name, ()))

    def emit(self, event: str, **payload) -> EventEmission:
        name = normalize_event_name(event)
        if name is None:
            raise ValueError(f"unknown event: {event}")
        return EventEmission(event=name, payload=dict(payload), hooks=self.handlers_for(name))

    def all(self) -> dict[str, list[EventHook]]:
        return {event: list(hooks) for event, hooks in self._hooks.items() if hooks}
