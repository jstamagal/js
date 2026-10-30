"""The retry budget every model request runs under.

The OpenAI and Anthropic SDK clients retry nothing (model_client._open_stream
sets their max_retries to 0). `ModelCaller` in turn_call.py retries a turn's
requests under this budget, interleaved with overflow recovery and max-output
recovery. `call` retries any other model request (compaction summaries, the
login test) under the same budget.
"""

from __future__ import annotations

import asyncio
import email.utils
import math
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import ai

from . import output
from . import settings as _settings
from . import stream_transport

@dataclass(frozen=True)
class Budget:
    """`attempts` retries after the first request; a Retry-After wait longer
    than `max_wait` seconds fails at once (0: any wait is honoured)."""

    attempts: int
    max_wait: float
    backoff_start: float = field(default_factory=lambda: _settings.default_value("RETRY_BACKOFF_START"))
    backoff_multiplier: float = field(default_factory=lambda: _settings.default_value("RETRY_BACKOFF_MULTIPLIER"))
    backoff_max: float = field(default_factory=lambda: _settings.default_value("RETRY_BACKOFF_MAX"))
    backoff_jitter: float = field(default_factory=lambda: _settings.default_value("RETRY_BACKOFF_JITTER"))


    @classmethod
    def from_settings(cls, settings: dict | None) -> Budget:
        return cls(
            backoff_start=float(_settings.knob(settings, "RETRY_BACKOFF_START")),
            backoff_multiplier=float(_settings.knob(settings, "RETRY_BACKOFF_MULTIPLIER")),
            backoff_max=float(_settings.knob(settings, "RETRY_BACKOFF_MAX")),
            backoff_jitter=float(_settings.knob(settings, "RETRY_BACKOFF_JITTER")),
            attempts=int(_settings.knob(settings, "RETRY_COUNT_MAX") or 0),
            max_wait=float(_settings.knob(settings, "RETRY_WAIT_MAX") or 0),
        )

    def too_long(self, wait: float | None) -> bool:
        # 0 means "honour any wait", but not one that never ends: an infinite
        # or NaN Retry-After would sleep the turn forever.
        if wait is not None and not math.isfinite(wait):
            return True
        return wait is not None and 0 < self.max_wait < wait


def idle_seconds(settings: dict | None) -> float | None:
    """STREAM_IDLE_TIMEOUT as `stream_model_async` takes it: None for
    no watchdog."""
    return float(_settings.knob(settings, "STREAM_IDLE_TIMEOUT") or 0) or None


def backoff(attempt: int, budget: Budget | None = None) -> float:
    """Exponential with jitter: 1s, 2s, 4s ... capped."""
    budget = budget if budget is not None else Budget.from_settings(None)
    base = min(budget.backoff_start * budget.backoff_multiplier ** attempt, budget.backoff_max)
    return base + random.uniform(0, budget.backoff_jitter)


def _error_chain(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and all(current is not seen for seen in chain):
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def _response_headers(err: BaseException) -> Any:
    response = getattr(getattr(err, "http_context", None), "response", None)
    if response is None:
        response = getattr(err, "response", None)
    headers = getattr(response, "headers", None)
    return headers if headers is not None else getattr(err, "headers", None)


def retry_after_seconds(exc: BaseException) -> float | None:
    """The wait the failed response asked for: its `retry-after-ms` header,
    else `Retry-After` as seconds or an HTTP date. None when it names none."""
    for err in _error_chain(exc):
        headers = _response_headers(err)
        if headers is None:
            continue
        try:
            millis = headers.get("retry-after-ms")
            after = headers.get("retry-after")
        except Exception:  # noqa: BLE001 — a header object without .get names no wait
            continue
        if millis:
            try:
                return max(0.0, float(millis) / 1000)
            except ValueError:
                pass
        if after:
            try:
                return max(0.0, float(after))
            except ValueError:
                pass
            try:
                when = email.utils.parsedate_to_datetime(after)
            except (TypeError, ValueError):
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
            return max(0.0, (when - datetime.now(UTC)).total_seconds())
    return None


def announce(n: int, budget: Budget, delay: float, exc: BaseException) -> None:
    """Tell the sink which retry will wait, and the failure it follows."""
    output.emit(output.event("retry_wait", n=n, attempts=budget.attempts, delay=delay,
                             error=exc, label=stream_transport._role.get().label))


def gave_up(retries: int) -> None:
    """Tell the sink the caller stopped retrying a retryable failure."""
    output.emit(output.event("retry_gave_up", n=retries, label=stream_transport._role.get().label))


_sleep = asyncio.sleep


async def wait(n: int, budget: Budget, delay: float, exc: BaseException) -> None:
    """Announce retry `n` and wait `delay` seconds before it, a `retry_tick`
    with the seconds left after each second of it. Every retry of a model
    call waits here; ^C ends the wait like any other."""
    announce(n, budget, delay, exc)
    status = stream_transport._role.get().status
    if status is not None:
        status.retry_wait(n, budget.attempts, delay)
    left = delay
    try:
        while left > 0:
            step = min(1.0, left)
            await _sleep(step)
            left -= step
            output.emit(output.event("retry_tick", left=left))
    finally:
        if status is not None:
            status.retry_done()
        output.emit(output.event("retry_done"))


async def call[T](request: Callable[[], Awaitable[T]], budget: Budget) -> T:
    """Await `request()`, sending it again after each retryable provider
    failure until `budget` is spent."""
    retries = 0
    while True:
        try:
            return await request()
        except ai.ProviderAPIError as exc:
            asked = retry_after_seconds(exc)
            if not exc.is_retryable:
                raise
            if retries >= budget.attempts or budget.too_long(asked):
                gave_up(retries)
                raise
            delay = asked if asked is not None else backoff(retries, budget)
            retries += 1
            await wait(retries, budget, delay, exc)
