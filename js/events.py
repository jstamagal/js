"""Typed event names and hook registration for the ircII-style command layer."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field


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
class EventHandlerResult:
    hook: EventHook
    lines: list[str] = field(default_factory=list)
    error: str | None = None
    changed: bool = False
    changed_keys: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class EventEmission:
    event: str
    payload: dict
    hooks: list[EventHook]
    results: list[EventHandlerResult] = field(default_factory=list)
    dispatch_skipped: bool = False


EventHandlerDispatcher = Callable[[EventHook, EventEmission], EventHandlerResult]


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

    Handlers are raw command text. A caller can install a dispatcher to interpret
    that text; emit catches dispatcher failures and records them on the returned
    emission so handler errors never abort the runtime event source.
    """

    def __init__(self, dispatcher: EventHandlerDispatcher | None = None) -> None:
        self._hooks: dict[str, list[EventHook]] = {event: [] for event in CANONICAL_EVENT_NAMES}
        self._dispatcher = dispatcher
        self._dispatch_depth = 0

    def set_dispatcher(self, dispatcher: EventHandlerDispatcher | None) -> None:
        self._dispatcher = dispatcher

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
        hooks = self.handlers_for(name)
        emission = EventEmission(event=name, payload=dict(payload), hooks=hooks)
        if self._dispatcher is None or not hooks:
            return emission
        if self._dispatch_depth > 0:
            return EventEmission(
                event=name,
                payload=dict(payload),
                hooks=hooks,
                dispatch_skipped=True,
            )

        self._dispatch_depth += 1
        try:
            for hook in hooks:
                try:
                    result = self._dispatcher(hook, emission)
                except Exception as e:  # noqa: BLE001 - hook failures are data
                    result = EventHandlerResult(
                        hook=hook,
                        error=f"{type(e).__name__}: {e}",
                    )
                emission.results.append(result)
        finally:
            self._dispatch_depth -= 1
        return emission

    def all(self) -> dict[str, list[EventHook]]:
        return {event: list(hooks) for event, hooks in self._hooks.items() if hooks}
