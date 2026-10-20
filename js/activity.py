"""What this process is busy with right now, for progress lines.

Three kinds of work count: a model request (`model_client.stream_model_async`
is `tracked`, so a turn's call, a compaction summary and a subagent's call
all are), the wait before a model request is sent again (`retry.call`), and
a tool call (`runtime` dispatch).
"""

from __future__ import annotations

import functools
import itertools
import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

MODEL = "model"
RETRY = "retry"
TOOL = "tool"


@dataclass(frozen=True)
class Running:
    """One piece of work: its kind, the tool's name for a tool call, when it
    started, and for a retry wait how long it waits."""

    kind: str
    started: float
    name: str = ""
    seconds: float = 0.0


_lock = threading.Lock()
_running: dict[int, Running] = {}
_keys = itertools.count(1)


@contextmanager
def _held(running: Running) -> Iterator[None]:
    key = next(_keys)
    with _lock:
        _running[key] = running
    try:
        yield
    finally:
        with _lock:
            _running.pop(key, None)


def model_call() -> Any:
    """One model request, from before it is sent until its reply is in or it fails."""
    return _held(Running(MODEL, time.monotonic()))


def retry_wait(seconds: float) -> Any:
    """The wait before a failed model request is sent again."""
    return _held(Running(RETRY, time.monotonic(), seconds=seconds))


def tool_call(name: str) -> Any:
    """One tool call while it runs."""
    return _held(Running(TOOL, time.monotonic(), name=name))


def running() -> list[Running]:
    """Everything running now, oldest first."""
    with _lock:
        return sorted(_running.values(), key=lambda item: item.started)


def tracked[T](call: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
    """`call`, an async model request, held as a `model_call()` while it runs."""

    @functools.wraps(call)
    async def wrapped(*args: Any, **kwargs: Any) -> T:
        with model_call():
            return await call(*args, **kwargs)

    return wrapped
