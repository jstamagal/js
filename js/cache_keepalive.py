"""Keep the main conversation's prompt cache warm while the REPL sits idle.

Anthropic drops a prompt cache entry once it goes unread for its lifetime
(`COMPACTION_CACHE_TTL`: 300 seconds by default, 3600 on the 1h tier), and
the next request then writes the whole prefix again. While the REPL waits for
input, `CacheKeepalive` resends the last main-conversation request exactly as
it went out, with a one-token output cap, just before that lifetime runs out:
the replay reads the whole prefix from the cache, which renews the entry, and
bills a cache read plus one output token.

A refresh goes out at 90% of the lifetime, at least ten seconds before it
ends. Refreshing stops when:

- the session has been idle `CACHE_KEEPALIVE_IDLE_MAX` seconds. With
  `CACHE_KEEPALIVE_COMPACTION` on, and the context at least
  `CACHE_KEEPALIVE_COMPACTION_TOKENS_MIN` tokens, the history is compacted
  first, so the request that ends the idle stretch writes a small prefix;
- a refresh reads less than half its prompt from the cache: the replay does
  not match what the provider cached, and every further one would be a write;
- a refresh fails, or the cache already ran out before one was sent (the
  machine slept);
- the model or provider changed, or the history was rewritten.

Only Claude routes are refreshed: the Anthropic wire, and a Claude model
behind an OpenAI-shaped endpoint such as CLIProxyAPI, which places the cache
breakpoints itself. OpenAI's cache is automatic, with a retention the
provider decides, and its Responses API takes no output cap under 16, so no
GPT route is refreshed. A request with a fixed thinking budget
(`THINKING_BUDGET`) is not replayed: a one-token cap changes the thinking
the request asks for, and with it what the cache holds.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from . import (
    messages as msgs,
    model_client,
    routing,
    settings as _settings,
    stream_transport,
    usage as usage_mod,
)

# How often the idle loop looks at the clock, in seconds.
POLL_S = 5.0
# A refresh goes out at this fraction of the cache lifetime...
REFRESH_FRACTION = 0.9
# ...and at least this many seconds before the lifetime ends.
REFRESH_MARGIN_S = 10
# A refresh that reads less than this share of its prompt from the cache missed.
HIT_SHARE = 0.5


@dataclass(frozen=True)
class CachedRequest:
    """One main-conversation model request as it went out: everything
    `model_client.stream_model_async` was given but its callbacks and output
    cap. ``sent_at`` is when it was sent; ``meter`` the usage meter it was
    charged under."""

    model: str
    provider_id: str | None
    base_url: str | None
    api_key: str | None
    effort: Any
    thinking_budget: int | None
    headers: Any
    extra: Any
    sampling: Any
    cache_key: str | None
    settings: dict | None
    stream_limits: Any
    messages: tuple
    tools: tuple | None
    sent_at: float
    meter: usage_mod.Meter | None = None


def remember(context: Any, request: Any, *, messages: list, tools: list | None,
             stream_limits: Any, now: float | None = None) -> None:
    """Record on ``context`` the request a turn is about to send. ``request``
    is the turn's `turn_call.ModelRequest`."""
    context.cached_request = CachedRequest(
        model=request.model, provider_id=request.provider_id, base_url=request.base_url,
        api_key=request.api_key, effort=request.effort, thinking_budget=request.thinking_budget,
        headers=request.headers, extra=request.extra, sampling=request.sampling,
        cache_key=request.cache_key, settings=request.settings, stream_limits=stream_limits,
        messages=tuple(messages), tools=tuple(tools) if tools else None,
        sent_at=time.time() if now is None else now, meter=usage_mod.current_meter(),
    )


def forget(context: Any) -> None:
    """The history was rewritten: the last request no longer leads the next."""
    context.cached_request = None


def not_replayable(request: CachedRequest) -> str | None:
    """Why ``request`` is not refreshed, or None when it is."""
    wire = routing.resolve_wire(request.provider_id, base_url=request.base_url, model_id=request.model)
    if wire.anthropic:
        if not _settings.knob(request.settings, "ANTHROPIC_PROMPT_CACHE"):
            return msgs.KEEPALIVE_WHY_CACHE_OFF.words()
    elif "claude" not in request.model.lower():
        return msgs.KEEPALIVE_WHY_NOT_CLAUDE.words(model=request.model)
    if request.thinking_budget is not None:
        return msgs.KEEPALIVE_WHY_BUDGET.words()
    return None


def refresh_delay(ttl: int) -> float | None:
    """Seconds after a cache read to refresh an entry that lives ``ttl``
    seconds; None when it lives too briefly to refresh."""
    if ttl <= REFRESH_MARGIN_S:
        return None
    return min(ttl * REFRESH_FRACTION, ttl - REFRESH_MARGIN_S)


async def replay(request: CachedRequest) -> model_client.ModelStreamResult:
    """Send ``request`` again with a one-token output cap."""
    pending = model_client.stream_model_async(
        model_id=request.model, provider_id=request.provider_id, provider_base_url=request.base_url,
        provider_api_key=request.api_key, messages=list(request.messages),
        tools=list(request.tools) if request.tools else None, max_output_tokens=1,
        reasoning_effort=request.effort, on_text=lambda _chunk: None, thinking_budget=request.thinking_budget,
        provider_headers=request.headers, provider_extra=request.extra, sampling=request.sampling,
        cache_key=request.cache_key, settings=request.settings, stream_limits=request.stream_limits,
    )
    return await pending if inspect.isawaitable(pending) else pending


def hit(usage: Any) -> bool:
    """Whether a refresh read most of its prompt from the cache."""
    prompt = int(getattr(usage, "input_tokens", 0) or 0)
    read = int(getattr(usage, "cache_read_tokens", 0) or 0)
    return prompt > 0 and read >= prompt * HIT_SHARE


class CacheKeepalive:
    """The idle refresher of one REPL session.

    ``context`` is the main conversation's ToolContext, whose
    ``cached_request`` the turns record. ``live`` returns the live settings;
    ``busy`` is true while a turn runs or waits in the queue; ``route``
    returns what names the live model and provider, so a switch stops the
    refreshing; ``compact`` queues the idle compaction and is called at most
    once per idle stretch. ``say`` prints a line to the scrollback.
    """

    def __init__(self, *, context: Any, live: Callable[[], dict], busy: Callable[[], bool],
                 route: Callable[[], Any], compact: Callable[[], None],
                 say: Callable[[msgs.Said], None] = msgs.say_said,
                 clock: Callable[[], float] = time.time,
                 send: Callable[[CachedRequest], Any] = replay,
                 telemetry: Any = None) -> None:
        self._context = context
        self._live = live
        self._busy = busy
        self._route = route
        self._compact = compact
        self._say = say
        self._clock = clock
        self._send = send
        self._telemetry = telemetry
        self._request: CachedRequest | None = None
        self._route_at_start: Any = None
        self._read_at = 0.0
        self._stopped = True
        self.next_at: float | None = None
        self.refreshes = 0

    async def run(self) -> None:
        """Look at the clock every POLL_S seconds until cancelled."""
        while True:
            await asyncio.sleep(POLL_S)
            await self.step()

    def _stop(self, reason: str | None = None) -> None:
        self._stopped = True
        self.next_at = None
        if reason is not None:
            self._event("cache_keepalive_stop", reason=reason)
            self._say(msgs.KEEPALIVE_STOPPED.said(reason=reason))

    def _event(self, name: str, **fields: Any) -> None:
        if self._telemetry is not None:
            self._telemetry.event(name, **fields)

    async def step(self) -> None:
        """One look at the clock: start tracking a new request, refresh the
        cache when it is due, or end the idle stretch."""
        live = self._live()
        request = getattr(self._context, "cached_request", None)
        if not _settings.knob(live, "CACHE_KEEPALIVE") or request is None or self._busy():
            self.next_at = None
            return
        if request is not self._request:
            self._request = request
            self._route_at_start = self._route()
            self._read_at = request.sent_at
            self.refreshes = 0
            reason = not_replayable(request)
            self._stopped = reason is not None
            if reason is not None:
                self._event("cache_keepalive_skip", reason=reason)
        if self._stopped:
            self.next_at = None
            return
        ttl = int(_settings.knob(live, "COMPACTION_CACHE_TTL") or 0)
        delay = refresh_delay(ttl)
        if delay is None:
            self.next_at = None
            return
        due = self._read_at + delay
        self.next_at = due
        now = self._clock()
        if now < due:
            return
        idle_max = int(_settings.knob(live, "CACHE_KEEPALIVE_IDLE_MAX") or 0)
        if idle_max <= 0:
            self._stop()
            return
        if now - request.sent_at >= idle_max:
            self._end_idle(live, request, now)
            return
        if now >= self._read_at + ttl:
            self._stop(msgs.KEEPALIVE_WHY_EXPIRED.words(seconds=int(now - self._read_at)))
            return
        if self._route() != self._route_at_start:
            self._stop()
            return
        await self._refresh(request, now)

    def _end_idle(self, live: dict, request: CachedRequest, now: float) -> None:
        idle = msgs.duration(now - request.sent_at)
        tokens = int(getattr(self._context, "last_prompt_tokens", 0) or 0) + int(
            getattr(self._context, "last_output_tokens", 0) or 0)
        compacting = (bool(_settings.knob(live, "CACHE_KEEPALIVE_COMPACTION"))
                      and tokens >= int(_settings.knob(live, "CACHE_KEEPALIVE_COMPACTION_TOKENS_MIN") or 0))
        self._stop(msgs.KEEPALIVE_WHY_IDLE.words(idle=idle, refreshes=self.refreshes))
        if compacting:
            self._event("cache_keepalive_compact", context_tokens=tokens)
            self._compact()

    async def _refresh(self, request: CachedRequest, now: float) -> None:
        # Charged to the sessions the request itself was charged to.
        meter_token = usage_mod.start(usage_mod.Meter(request.meter.sessions)) if request.meter else None
        try:
            try:
                with stream_transport.net_role(msgs.KEEPALIVE_ROLE.words()):
                    result = await self._send(request)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a failed refresh ends the refreshing, never the REPL
                self._stop(msgs.KEEPALIVE_WHY_FAILED.words(error=msgs.of_error(exc)))
                return
            usage = result.usage
            call = (usage_mod.record(usage, model=request.model, provider_id=request.provider_id)
                    or usage_mod.call_usage(usage, model=request.model, provider_id=request.provider_id))
        finally:
            if meter_token is not None:
                usage_mod.stop(meter_token)
        prompt = int(getattr(usage, "input_tokens", 0) or 0)
        read = int(getattr(usage, "cache_read_tokens", 0) or 0)
        self._event("cache_keepalive", model=request.model, prompt_tokens=prompt, cached_tokens=read,
                    output_tokens=call.output_tokens, cost=call.cost)
        if getattr(self._context, "cached_request", None) is not request:
            # A turn started while the refresh was out; it owns the cache now.
            return
        if not hit(usage):
            self._stop(msgs.KEEPALIVE_WHY_MISSED.words(read=read, prompt=prompt))
            return
        self.refreshes += 1
        self._read_at = now
        self._context.last_request_at = now
        delay = refresh_delay(int(_settings.knob(self._live(), "COMPACTION_CACHE_TTL") or 0))
        self.next_at = now + delay if delay is not None else None
        cost = usage_mod.format_dollars(call.cost) if call.cost is not None else msgs.KEEPALIVE_UNPRICED.words()
        stream_transport.say(2, msgs.NET_CACHE_KEEPALIVE.said(
            read=read, cost=cost, next=clock_text(self.next_at)))


def clock_text(at: float | None) -> str:
    """``at`` as the status bar's clock shows it, HH:MM local time."""
    return time.strftime("%H:%M", time.localtime(at)) if at is not None else "-"
