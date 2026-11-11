"""Render the network channel's notices and failures."""

from __future__ import annotations

import re
import socket
import sys
from dataclasses import dataclass

import httpx
import httpx2

from . import fset
from . import messages as msgs


@dataclass(frozen=True)
class Failure:
    error: BaseException

    def said(self) -> msgs.Said:
        return describe_failure(self.error)

    def __str__(self) -> str:
        return str(self.said())


@dataclass(frozen=True)
class LabelledLine:
    label: str
    line: object


def draw(level: int, line: str, *, fallback: bool = False) -> None:
    """Draw a network notice through the installed channel or its stderr fallback."""
    from . import stream_transport

    sink = stream_transport._sink
    if sink is None and fallback:
        print(msgs.for_stream(_net_line(line), sys.stderr), file=sys.stderr)
    elif sink is not None and (stream_transport.net_level() or 0) >= level:
        try:
            sink.emit(_net_line(line))
        except Exception:  # noqa: BLE001 — a display failure never fails the request
            pass


def always(line: object) -> None:
    """Draw a notice whatever NET_DISPLAY says: through the installed channel,
    else on stderr."""
    from . import stream_transport

    sink = stream_transport._sink
    if sink is not None:
        sink.emit(_net_line(line))
    else:
        print(msgs.for_stream(_net_line(line), sys.stderr), file=sys.stderr, flush=True)


# The line of the wait being counted, from its seconds; None when none.
_countdown = None


def countdown_start(line_for, seconds: float) -> None:
    """Count a wait (a retry down, a first byte up) on one stderr row,
    redrawn in place and erased when it ends. Only on a terminal with no
    channel installed (`js -p`): the REPL's status bar counts both."""
    global _countdown
    from . import stream_transport

    try:
        tty = sys.stderr.isatty()
    except (AttributeError, ValueError, OSError):
        tty = False
    if stream_transport._sink is not None or not tty:
        return
    _countdown = line_for
    countdown_tick(seconds)


def counting() -> bool:
    """Whether a wait is being counted on its stderr row."""
    return _countdown is not None


def countdown_tick(seconds: float) -> None:
    if _countdown is not None:
        sys.stderr.write("\r\033[2K" + msgs.for_stream(_net_line(_countdown(seconds)), sys.stderr))
        sys.stderr.flush()


def countdown_end() -> None:
    global _countdown
    if _countdown is not None:
        _countdown = None
        sys.stderr.write("\r\033[2K")
        sys.stderr.flush()


def _net_line(line: str) -> str:
    """A network line on the screen: its entry's runs."""
    if isinstance(line, LabelledLine):
        inner = line.line.said() if isinstance(line.line, Failure) else line.line
        line = msgs.NET_LABELLED.said(label=line.label, text=inner if isinstance(inner, msgs.Said) else str(inner))
    elif isinstance(line, Failure):
        line = line.said()
    said = line if isinstance(line, msgs.Said) else msgs.NET_LINE.said(text=line)
    return fset.to_ansi(said.message.runs(**said.fields))


def describe_failure(exc: BaseException) -> msgs.Said:
    """One line naming a failed model request: status, js's own timeout,
    DNS, timeout or connect. A `said()` entry: NET_STATUS_FAILURE or
    NET_FAILURE."""
    seen: set[int] = set()
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    from . import model_client

    outage = outage_failure(chain)
    if outage is not None:
        return outage
    for err in chain:
        status = model_client.error_status(err)
        if status is None:
            status = getattr(err, "status_code", None)
        if isinstance(status, int):
            return msgs.NET_STATUS_FAILURE.said(status=msgs.NET_FAILURE_STATUS.said(status=status),
                                                reason=_capped(model_client.error_message(err)))
    for err in chain:
        if isinstance(err, model_client.RequestTimeoutError):
            return _failure(msgs.NET_TIMEOUT, _one_line(err))
    host = next((h for h in (_request_host(e) for e in chain) if h), "")
    for err in chain:
        text = str(err)
        if isinstance(err, socket.gaierror) or any(needle in text for needle in (
            "Name or service not known", "nodename nor servname",
            "Temporary failure in name resolution", "No address associated with hostname",
        )):
            return _failure(msgs.NET_DNS_FAILURE, host)
    for err in chain:
        if isinstance(err, (httpx2.TimeoutException, httpx.TimeoutException, TimeoutError)):
            return _failure(msgs.NET_TIMEOUT, host)
    for err in chain:
        if isinstance(err, (httpx2.ConnectError, httpx.ConnectError, ConnectionError)):
            return _failure(msgs.NET_CONNECT_FAILED, f"{host} {_one_line(err)}".strip())
    return msgs.NET_FAILURE.said(kind=msgs.NET_FAILURE_TYPE.said(name=type(exc).__name__), detail=_one_line(exc))


def is_outage(failure: msgs.Said) -> bool:
    return failure.message in (
        msgs.NET_OUTAGE_OVERLOADED, msgs.NET_OUTAGE_UNAVAILABLE, msgs.NET_OUTAGE_EMPTY,
        msgs.NET_OUTAGE_ACCOUNT, msgs.NET_OUTAGE_ACCOUNT_OVERLOAD,
    )


def outage_failure(chain: list[BaseException]) -> msgs.Said | None:
    """A diagnosis based on provider metadata, never on a gateway status alone."""
    from . import model_client

    details = []
    statuses = []
    identities = []
    for err in chain:
        details.extend((str(err), str(getattr(err, "code", "")),
                        str(getattr(err, "body", ""))))
        statuses.append(model_client.error_status(err) or getattr(err, "status_code", None))
        identities.extend((str(getattr(err, "provider", "")), _request_host(err)))
    text = " ".join(details).lower()
    identity = " ".join(identities).lower()
    # The proxy's explicit upstream account pool outranks SDK transport metadata.
    upstream = re.search(r"providers=(codex|openai|anthropic)\b", text)
    if upstream:
        identity = upstream.group(1)
    if "openai" in identity or "codex" in identity or "chatgpt.com" in identity:
        service = "OpenAI"
    elif "anthropic" in identity:
        service = "Anthropic"
    else:
        service = "The upstream service"
    overloaded = any(code in text for code in (
        "server_is_overloaded", "overloaded_error", "servers are currently overloaded",
    )) or 529 in statuses
    if "auth_unavailable" in text:
        if overloaded:
            return msgs.NET_OUTAGE_ACCOUNT_OVERLOAD.said(service=service if service != "The upstream service" else "the upstream service")
        return msgs.NET_OUTAGE_ACCOUNT.said()
    if "empty_stream" in text or "upstream stream closed before first payload" in text:
        return msgs.NET_OUTAGE_EMPTY.said(service=service)
    if overloaded:
        return msgs.NET_OUTAGE_OVERLOADED.said(service=service)
    if 503 in statuses or "service_unavailable" in text:
        return msgs.NET_OUTAGE_UNAVAILABLE.said(service=service, status=" (HTTP 503)" if 503 in statuses else "")
    return None


_DETAIL_WIDTH = 160


def _one_line(err: BaseException) -> str:
    """``err`` as one capped line: a provider error often carries a
    multi-line JSON body, and the failure is one banner line. Whitespace runs,
    newlines included, become one space, so a JSON body still reads."""
    return _capped(str(err))


def _capped(text: str) -> str:
    line = " ".join(text.split())
    return line if len(line) <= _DETAIL_WIDTH else line[:_DETAIL_WIDTH - 1] + "…"


def _failure(kind: msgs.Message, detail: str) -> msgs.Said:
    """A failure line: the kind of failure, then what it names."""
    return msgs.NET_FAILURE.said(kind=kind.said(), detail=detail)


def _request_host(err: BaseException) -> str:
    try:
        request = err.request  # httpx2 raises RuntimeError when none is attached
    except Exception:  # noqa: BLE001
        return ""
    return getattr(getattr(request, "url", None), "host", "") or ""
