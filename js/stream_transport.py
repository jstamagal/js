"""The model request's HTTP layer: response ownership and the network channel.

httpx2 2.12's default response stream closes the pool entry but not its active
byte iterator (pydantic/httpx2#1195). This response-local wrapper closes both.
Recheck the adapter when that upstream iterator ownership is repaired.

The same wrapper sees every response byte, which is what the `ui.net` channel
counts. The channel prints only while a sink is installed (the async REPL
screen); everywhere else each hook is a no-op.
"""

from __future__ import annotations

import contextlib
import contextvars
import socket
import sys
import time
from collections.abc import AsyncGenerator, AsyncIterable, AsyncIterator, Callable, Iterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx2
from httpx2._client import BoundAsyncStream
from httpx2._transports.default import AsyncResponseStream

from . import colors as C


@dataclass(frozen=True)
class NetSink:
    """Where network lines go and how many of them: `level()` is read live."""

    level: Callable[[], int]
    emit: Callable[[str], None]


class FailureHold:
    """The last failed request of a caller that retries: its failure line waits
    here until the caller gives up (`report_held_failure`) or the next request
    starts, which drops it."""

    def __init__(self) -> None:
        self.pending: BaseException | None = None


@dataclass(frozen=True)
class NetRole:
    """Who is calling: the main turn (label ""), "Subagent N", or "Compacting".
    `status` is the TurnStatus the response bytes are counted into. With a
    `hold`, failures wait for the caller's verdict instead of printing."""

    label: str = ""
    agent: str = ""
    status: Any = None
    hold: FailureHold | None = None


_sink: NetSink | None = None
_role: contextvars.ContextVar[NetRole] = contextvars.ContextVar("js_net_role", default=NetRole())


def install_sink(sink: NetSink | None) -> None:
    global _sink
    _sink = sink


def net_level() -> int | None:
    """The live `ui.net` level while a sink is installed, else None."""
    if _sink is None:
        return None
    try:
        return int(_sink.level())
    except Exception:  # noqa: BLE001 — a bad setting silences the channel, never the turn
        return 0


def set_role(label: str = "", *, agent: str = "", status: Any = None,
             retries: bool = False) -> contextvars.Token:
    """Name the caller of the model requests this task makes from here on.
    `retries` means the caller retries failed requests and reports the one it
    gives up on with `report_held_failure`."""
    hold = FailureHold() if retries else None
    return _role.set(NetRole(label=label, agent=agent, status=status, hold=hold))


def report_held_failure() -> None:
    """The caller gave up: print its last request failure at level 1."""
    hold = _role.get().hold
    if hold is None or hold.pending is None:
        return
    exc, hold.pending = hold.pending, None
    say_for_caller(1, describe_failure(exc))


def say_or_print(level: int, text: str) -> None:
    """A network line that predates the channel: the channel decides while a
    sink is installed; otherwise it prints to stderr as a `***` line."""
    if _sink is None:
        print(f"*** {text}", file=sys.stderr)
    else:
        say(level, text)


def say_for_caller(level: int, text: str) -> None:
    """`say`, prefixed with the calling role's label ("Subagent 2: ...")."""
    say(level, _labelled(_role.get(), text))


def _labelled(role: NetRole, text: str) -> str:
    return f"{role.label}: {text}" if role.label else text


def reset_role(token: contextvars.Token) -> None:
    _role.reset(token)


@contextlib.contextmanager
def net_role(label: str = "", *, agent: str = "", status: Any = None) -> Iterator[None]:
    token = set_role(label, agent=agent, status=status)
    try:
        yield
    finally:
        reset_role(token)


def say(level: int, text: str) -> None:
    """Print one `***` network line when the channel is at `level` or above."""
    if _sink is not None and (net_level() or 0) >= level:
        try:
            _sink.emit(_banner(text))
        except Exception:  # noqa: BLE001 — a display failure never fails the request
            pass


def _host(url: str) -> str:
    return urlsplit(url).hostname or url


def _banner(text: str) -> str:
    return f"{C.BR_YELLOW}*** {text}{C.RESET}"


def describe_failure(exc: BaseException) -> str:
    """One line naming a failed model request: status, DNS, timeout or connect."""
    seen: set[int] = set()
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    for err in chain:
        status = getattr(getattr(err, "http_context", None), "status_code", None)
        if status is None:
            status = getattr(err, "status_code", None)
        if isinstance(status, int):
            first_line = next(iter(str(err).splitlines()), "")
            reason = str(getattr(err, "code", None) or getattr(err, "type", None) or first_line[:80])
            return f"{C.BR_RED}{status}{C.BR_YELLOW} {reason}".rstrip()
    for err in chain:
        text = str(err)
        if isinstance(err, socket.gaierror) or any(needle in text for needle in (
            "Name or service not known", "nodename nor servname",
            "Temporary failure in name resolution", "No address associated with hostname",
        )):
            host = next((h for h in (_request_host(e) for e in chain) if h), "")
            return f"{C.BR_RED}DNS failure{C.BR_YELLOW}: {host}".rstrip(": ")
    for err in chain:
        if isinstance(err, (httpx2.TimeoutException, TimeoutError)):
            host = next((h for h in (_request_host(e) for e in chain) if h), "")
            return f"{C.BR_RED}Timeout{C.BR_YELLOW}: {host}".rstrip(": ")
    for err in chain:
        if isinstance(err, (httpx2.ConnectError, ConnectionError)):
            host = next((h for h in (_request_host(e) for e in chain) if h), "")
            return f"{C.BR_RED}Connection failed{C.BR_YELLOW}: {host} {err}".strip()
    return f"{C.BR_RED}{type(exc).__name__}{C.BR_YELLOW}: {exc}"


def _request_host(err: BaseException) -> str:
    try:
        request = err.request  # httpx2 raises RuntimeError when none is attached
    except Exception:  # noqa: BLE001
        return ""
    return getattr(getattr(request, "url", None), "host", "") or ""


class NetCall:
    """One model request on the network channel. Every hook is safe to call at
    any level; each decides for itself whether it prints."""

    def __init__(self, role: NetRole, url: str, model: str) -> None:
        self._role = role
        self._url = url
        self._model = model
        self._started = time.perf_counter()
        self._connected = False
        self._sent = False
        if role.hold is not None:
            role.hold.pending = None
        if role.status is not None:
            role.status.call_started()
        say(2, self._connecting_line())

    def _connecting_line(self) -> str:
        label, agent = self._role.label, self._role.agent
        if label == "Compacting":
            return f"Compacting: {self._model} via {self._url}"
        if label:
            suffix = f"  (agent={agent})" if agent else ""
            return f"{label}: connecting {self._url}{suffix}"
        return f"Connecting: {self._url}"

    def request_sent(self) -> None:
        """The transport is sending the request: the handshake clock starts here,
        after the SDK's own setup."""
        if not self._sent:
            self._sent = True
            self._started = time.perf_counter()

    def connected(self, host: str = "") -> None:
        if self._connected:
            return
        self._connected = True
        ms = int((time.perf_counter() - self._started) * 1000)
        who = f"{self._role.label}: connected" if self._role.label else "Connected"
        say(2, f"{who}: {host or _host(self._url)}  {ms}ms")

    def received(self, n: int) -> None:
        self.connected()
        if self._role.status is not None:
            self._role.status.add_bytes(n)

    def first_token(self) -> None:
        self.connected()

    def failed(self, exc: BaseException) -> None:
        if self._role.hold is not None:
            self._role.hold.pending = exc
            return
        say(1, _labelled(self._role, describe_failure(exc)))

    async def trace(self, event: str, info: dict[str, Any]) -> None:
        """httpcore2's `trace` request extension: the handshake is the connect."""
        if event in ("connection.start_tls.complete", "connection.connect_tcp.complete"):
            if event.endswith("connect_tcp.complete") and self._url.startswith("https:"):
                return
            self.connected()


_call: contextvars.ContextVar[NetCall | None] = contextvars.ContextVar("js_net_call", default=None)


def set_call(call: NetCall | None) -> contextvars.Token:
    return _call.set(call)


def reset_call(token: contextvars.Token) -> None:
    _call.reset(token)


def current_call() -> NetCall | None:
    """The request this task is making, for the transport hooks to feed."""
    return _call.get()


def begin_call(url: str, model: str) -> NetCall | None:
    """Open the network channel for one model request, or None with no sink."""
    if _sink is None:
        return None
    return NetCall(_role.get(), url, model)


class _OwnedCoreStream:
    def __init__(self, stream: Any, call: NetCall | None = None) -> None:
        self._stream = stream
        self._iterator: AsyncIterator[bytes] = aiter(stream)
        self._closed = False
        self._call = call

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self

    async def __anext__(self) -> bytes:
        chunk = await anext(self._iterator)
        if self._call is not None:
            self._call.received(len(chunk))
        return chunk

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if isinstance(self._iterator, AsyncGenerator):
                await self._iterator.aclose()
        finally:
            await self._stream.aclose()


@asynccontextmanager
async def own_responses(client: httpx2.AsyncClient, call: NetCall | None = None):
    """Close this model request's byte iterators when its stream scope exits.
    With a `call`, its handshake and response bytes feed the network channel."""
    async with AsyncExitStack() as cleanup:

        async def own(response: httpx2.Response) -> None:
            stream: AsyncIterable[bytes] = response.stream
            if isinstance(stream, BoundAsyncStream):
                stream = stream._stream
            if isinstance(stream, AsyncResponseStream):
                wrapper = _OwnedCoreStream(stream._httpcore_stream, call)
                stream._httpcore_stream = wrapper
                cleanup.push_async_callback(wrapper.aclose)

        async def trace(request: httpx2.Request) -> None:
            if call is not None:
                call.request_sent()
                request.extensions = {**request.extensions, "trace": call.trace}

        response_hooks = client.event_hooks["response"]
        request_hooks = client.event_hooks["request"] if call is not None else []
        response_hooks.append(own)
        request_hooks.append(trace)
        try:
            yield
        finally:
            response_hooks.remove(own)
            request_hooks.remove(trace)
