"""Invocation-wide model measurements and opt-in removal from saved model lists."""
from __future__ import annotations

import asyncio
import contextvars
import math
import time
from dataclasses import dataclass
from typing import Any

from . import logins


@dataclass(frozen=True)
class RemovalPolicy:
    slow_decode: float | None = None
    slow_ttft: float | None = None
    failure: bool = False

    @property
    def deadline(self) -> float:
        return self.slow_ttft if self.slow_ttft is not None else 300.0

    @classmethod
    def parse(cls, text: str) -> RemovalPolicy:
        values: dict[str, Any] = {}
        for entry in (text or 'slow_decode,slow_ttft,failure').split(','):
            name, sep, raw = entry.strip().partition(':')
            if name == 'failure' and not sep:
                values[name] = True
            elif name in ('slow_decode', 'slow_ttft'):
                value = float(raw) if sep else (8.0 if name == 'slow_decode' else 300.0)
                if not math.isfinite(value) or value <= 0:
                    raise ValueError('model health thresholds must be positive finite numbers')
                values[name] = value
            else:
                raise ValueError('--remove-if-fail expects slow_decode[:TPS],slow_ttft[:SECONDS],failure')
        return cls(**values)


@dataclass
class Measurement:
    tokens: int = 0
    decode_s: float = 0.0
    received: bool = False
    slow_ttft: bool = False
    failure: bool = False


class HealthSession:
    def __init__(self, policy: RemovalPolicy):
        self.policy = policy
        self.models: dict[tuple[str, str], Measurement] = {}

    def finish(self) -> None:
        from . import stream_transport
        for (provider, model), measured in self.models.items():
            reasons = []
            if measured.slow_ttft:
                reasons.append('slow_ttft')
            if measured.failure:
                reasons.append('failure')
            if (self.policy.slow_decode is not None and measured.received and measured.tokens > 0
                    and measured.decode_s > 0 and measured.tokens / measured.decode_s < self.policy.slow_decode):
                reasons.append('slow_decode')
            if reasons and logins.remove_cached_model(provider, model):
                stream_transport.say(1, f'Removed {provider}/{model}: {", ".join(reasons)}')


_session: contextvars.ContextVar[HealthSession | None] = contextvars.ContextVar('model_health', default=None)
_call: contextvars.ContextVar[HealthCall | None] = contextvars.ContextVar('model_health_call', default=None)


class HealthCall:
    def __init__(self, session: HealthSession, provider: str, model: str):
        self.session = session
        self.measured = session.models.setdefault((provider, model), Measurement())
        self.started: float | None = None
        self.first: float | None = None
        self.assistant = False
        self.timer: asyncio.TimerHandle | None = None
        self.expired = False

    def sent(self) -> None:
        if self.started is not None:
            return
        self.started = time.perf_counter()
        if self.session.policy.failure:
            task = asyncio.current_task()
            def expire():
                self.expired = True
                task.cancel()
            self.timer = asyncio.get_running_loop().call_later(self.session.policy.deadline, expire)

    def token(self, *, assistant: bool) -> None:
        self.sent()
        now = time.perf_counter()
        if self.first is None:
            self.first = now
            self.measured.received = True
            limit = self.session.policy.slow_ttft
            if limit is not None and now - self.started > limit:
                self.measured.slow_ttft = True
        if assistant:
            self.assistant = True
            if self.timer is not None:
                self.timer.cancel()

    async def run(self, pending):
        token = _call.set(self)
        try:
            result = await pending
            if not self.assistant and self.session.policy.failure and self.started is not None:
                remaining = self.session.policy.deadline - (time.perf_counter() - self.started)
                await asyncio.sleep(max(0, remaining))
                self.measured.failure = True
            if self.first is not None:
                self.measured.tokens += int(getattr(result.usage, 'output_tokens', 0) or 0)
                self.measured.decode_s += time.perf_counter() - self.first
            return result
        except asyncio.CancelledError:
            if not self.expired:
                raise
            self.measured.failure = not self.assistant
            raise TimeoutError(f'No assistant turn within {self.session.policy.deadline:g}s') from None
        finally:
            if self.timer is not None:
                self.timer.cancel()
            _call.reset(token)


def sent() -> None:
    if (call := _call.get()) is not None:
        call.sent()


def token(*, assistant: bool) -> None:
    if (call := _call.get()) is not None:
        call.token(assistant=assistant)


async def watch(pending, provider: str | None, model: str):
    session = _session.get()
    if session is None:
        return await pending
    return await HealthCall(session, provider or '', model).run(pending)
