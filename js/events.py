"""Typed event names and hook registration for the ircII-style command layer."""

from __future__ import annotations

import asyncio
import inspect
import threading
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import Any

from . import messages as msgs


CANONICAL_EVENT_NAMES: tuple[str, ...] = (
    "input",
    "prompt",
    "stream",
    "tool_call",
    "tool_result",
    "response",
    "turn_start",
    "turn_end",
    "mcp_log",
    "mcp_progress",
    "mcp_resource_updated",
    "mcp_catalog_collision",
    "error",
    "cancel",
    "session_start",
    "session_end",
    "pre_compact",
    "post_compact",
)

# The one event whose handlers can refuse what it announces: a refused tool
# call never runs, and the model reads the refusal as its result.
REFUSABLE_EVENTS = frozenset({"tool_call"})

_EVENT_SET = frozenset(CANONICAL_EVENT_NAMES)


@dataclass(frozen=True)
class EventHook:
    event: str
    handler: str
    suppress: bool = False


@dataclass(frozen=True)
class EventHandlerResult:
    hook: EventHook
    error: str | None = None
    # Set when the handler refused the event: the one line the model reads.
    refusal: str | None = None


@dataclass(frozen=True)
class EventEmission:
    event: str
    payload: dict
    hooks: list[EventHook]
    results: list[EventHandlerResult] = field(default_factory=list)
    dispatch_skipped: bool = False


EventHandlerDispatcher = Callable[[EventHook, EventEmission], EventHandlerResult]


@dataclass
class HandlerCall:
    """The handler running now: its hook, the emission it answers, and the
    refusal it records, if any."""

    hook: EventHook
    emission: EventEmission
    refusal: str | None = None


_CURRENT_CALL: ContextVar[HandlerCall | None] = ContextVar("js_event_handler_call", default=None)


def current_call() -> HandlerCall | None:
    """The handler call in progress on this thread of control; None outside one."""
    return _CURRENT_CALL.get()


def refusal_of(emission: object) -> str | None:
    """The first refusal a handler of ``emission`` recorded; None when none did."""
    for result in getattr(emission, "results", ()) or ():
        refusal = getattr(result, "refusal", None)
        if refusal:
            return refusal
    return None


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
        self._dispatch_depth: ContextVar[int] = ContextVar("js_event_dispatch_depth", default=0)

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
        if hook not in self._hooks[event]:  # the same handler twice would run twice
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
        if self._dispatch_depth.get() > 0:
            return EventEmission(
                event=name,
                payload=dict(payload),
                hooks=hooks,
                dispatch_skipped=True,
            )

        depth_token = self._dispatch_depth.set(self._dispatch_depth.get() + 1)
        try:
            for hook in hooks:
                call = HandlerCall(hook=hook, emission=emission)
                token = _CURRENT_CALL.set(call)
                try:
                    result = self._dispatcher(hook, emission)
                except Exception as e:  # noqa: BLE001 - hook failures are data
                    result = EventHandlerResult(
                        hook=hook,
                        error=f"{type(e).__name__}: {e}",
                    )
                finally:
                    _CURRENT_CALL.reset(token)
                if not isinstance(result, EventHandlerResult):
                    result = EventHandlerResult(
                        hook=hook,
                        error=f"invalid event handler result: {type(result).__name__}",
                    )
                if call.refusal and result.refusal is None and name in REFUSABLE_EVENTS:
                    result = replace(result, refusal=call.refusal)
                emission.results.append(result)
        finally:
            self._dispatch_depth.reset(depth_token)
        return emission

    def all(self) -> dict[str, list[EventHook]]:
        return {event: list(hooks) for event, hooks in self._hooks.items() if hooks}


class RefusableOnly:
    """The part of an `on` table a subagent's turn answers to: the handlers of
    the refusable events, so a tool_call guard vets a subagent's calls as it
    vets the parent's. Every other event emits to no handler."""

    def __init__(self, hooks: EventHooks | RefusableOnly) -> None:
        self._hooks = hooks

    def emit(self, event: str, **payload) -> EventEmission:
        name = normalize_event_name(event)
        if name is None:
            raise ValueError(f"unknown event: {event}")
        if name in REFUSABLE_EVENTS:
            return self._hooks.emit(name, **payload)
        return EventEmission(event=name, payload=dict(payload), hooks=[])


# A program that assembles js from its parts can hold each tool call until a
# decision arrives (from its screen, say). The callable gets the call as the
# tool_call event carries it ({"id", "name", "arguments"}, the arguments as
# JSON text). It returns None to let the call run, or the line the model reads
# as the refused call's result, directly or as an awaitable. js's own command
# line supplies none.
Approve = Callable[[dict], "str | None | Awaitable[str | None]"]


@dataclass(frozen=True)
class Approval:
    """An approval callable and the event loop of the turn that took it. The
    callable runs on that loop, so it can wait on the screen that loop serves;
    when that loop no longer runs, it runs on the caller's."""

    approve: Approve
    loop: asyncio.AbstractEventLoop
    _pending: set[asyncio.Task] = field(default_factory=set, init=False, repr=False, compare=False)
    _cancelled: threading.Event = field(default_factory=threading.Event, init=False, repr=False, compare=False)
    _lock: Any = field(default_factory=threading.Lock, init=False, repr=False, compare=False)

    def cancel(self) -> None:
        """Revoke this turn's pending decisions, including kernel bridge calls."""
        with self._lock:
            self._cancelled.set()
            pending = tuple(self._pending)
        for task in pending:
            task.get_loop().call_soon_threadsafe(task.cancel)

    async def _decide(self, call: dict) -> str | None:
        task = asyncio.current_task()
        assert task is not None
        with self._lock:
            if self._cancelled.is_set():
                return _failed(call, asyncio.CancelledError())
            self._pending.add(task)
        try:
            decision: Any = self.approve(call)
            if inspect.isawaitable(decision):
                decision = await decision
            if self._cancelled.is_set():
                return _failed(call, asyncio.CancelledError())
            if decision is not None and not isinstance(decision, str):
                raise TypeError("approval must return None or a refusal string")
            return decision
        except Exception as exc:  # noqa: BLE001 - a failed decision refuses the call
            return _failed(call, exc)
        finally:
            with self._lock:
                self._pending.discard(task)

    def _on_own_loop(self) -> bool:
        return self.loop.is_closed() or not self.loop.is_running()

    async def refusal(self, call: dict) -> str | None:
        """The refusal for ``call``; None when the call may run. A callable
        that raises refuses the call."""
        if self._on_own_loop() or asyncio.get_running_loop() is self.loop:
            return await self._decide(call)
        future = asyncio.run_coroutine_threadsafe(self._decide(call), self.loop)
        try:
            return await asyncio.wrap_future(future)
        except Exception as exc:  # noqa: BLE001
            return _failed(call, exc)

    def refusal_from_thread(self, call: dict) -> str | None:
        """``refusal`` from a thread that runs no event loop, such as the
        kernel's tool bridge: this thread waits for the decision."""
        if self._on_own_loop():
            return asyncio.run(self._decide(call))
        future = asyncio.run_coroutine_threadsafe(self._decide(call), self.loop)
        try:
            return future.result()
        except Exception as exc:  # noqa: BLE001 - includes a cancelled turn
            return _failed(call, exc)


def _failed(call: dict, exc: BaseException) -> str:
    return f"ERROR: {call.get('name')} was not run: the approval failed: {type(exc).__name__}: {msgs.of_error(exc)}"
