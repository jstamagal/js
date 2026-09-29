"""The retry budget every model request runs under.

The OpenAI and Anthropic SDK clients retry nothing (model_client._open_stream
sets their max_retries to 0). The turn loop in runtime.py retries a turn's
requests under this budget, interleaved with overflow recovery and max-output
recovery. `call` retries any other model request (compaction summaries, the
login test) under the same budget.
"""

from __future__ import annotations

import asyncio
import email.utils
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import ai

from . import messages as msgs
from . import settings as _settings
from . import stream_transport

@dataclass(frozen=True)
class Budget:
    """`attempts` retries after the first request; a Retry-After wait longer
    than `max_wait` seconds fails at once (0: any wait is honoured)."""

    attempts: int
    max_wait: float

    @classmethod
    def from_settings(cls, settings: dict | None) -> Budget:
        return cls(
            attempts=int(_settings.knob(settings, "runtime.retry_attempts") or 0),
            max_wait=float(_settings.knob(settings, "runtime.retry_max_wait_seconds") or 0),
        )

    def too_long(self, wait: float | None) -> bool:
        return wait is not None and 0 < self.max_wait < wait


def idle_seconds(settings: dict | None) -> float | None:
    """runtime.stream_idle_seconds as `stream_model_async` takes it: None for
    no watchdog."""
    return float(_settings.knob(settings, "runtime.stream_idle_seconds") or 0) or None


def backoff(attempt: int) -> float:
    """Exponential with jitter: 1s, 2s, 4s ... capped."""
    base = min(2 ** attempt, 16)
    return base + random.uniform(0, 1)


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
    """The network-channel line for retry `n`. A wait of 10s or more shows at
    level 1, so a long Retry-After does not look like a hang."""
    stream_transport.say_for_caller(
        1 if delay >= 10 else 3,
        msgs.RETRY_WAIT.text(n=n, of=budget.attempts, seconds=delay,
                             failure=stream_transport.describe_failure(exc)))


async def call[T](request: Callable[[], Awaitable[T]], budget: Budget) -> T:
    """Await `request()`, sending it again after each retryable provider
    failure until `budget` is spent."""
    retries = 0
    while True:
        try:
            return await request()
        except ai.ProviderAPIError as exc:
            wait = retry_after_seconds(exc)
            if not exc.is_retryable or retries >= budget.attempts or budget.too_long(wait):
                raise
            delay = wait if wait is not None else backoff(retries)
            retries += 1
            announce(retries, budget, delay, exc)
            await asyncio.sleep(delay)
