"""Render the network channel's notices and failures."""

from __future__ import annotations

import socket
import sys
from dataclasses import dataclass

import httpx2

from . import fset
from . import messages as msgs


@dataclass(frozen=True)
class Failure:
    error: BaseException

    def __str__(self) -> str:
        return describe_failure(self.error)


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


def _net_line(line: str) -> str:
    """A network line on the screen: its entry's runs, COLOR_WARN where the
    format sets no colour."""
    if isinstance(line, LabelledLine):
        inner = line.line
        text = fset.to_ansi(inner.message.body_runs(**inner.fields)) if isinstance(inner, msgs.Said) else str(inner)
        line = msgs.NET_LABELLED.said(label=line.label, text=text)
    elif isinstance(line, Failure):
        line = str(line)
    said = line if isinstance(line, msgs.Said) else msgs.NET_LINE.said(text=line)
    return fset.to_ansi(said.message.runs(whole="COLOR_WARN", **said.fields))


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
            return f"{fset.fg('COLOR_ERROR')}{status}{fset.fg('COLOR_WARN')} {reason}".rstrip()
    for err in chain:
        text = str(err)
        if isinstance(err, socket.gaierror) or any(needle in text for needle in (
            "Name or service not known", "nodename nor servname",
            "Temporary failure in name resolution", "No address associated with hostname",
        )):
            host = next((h for h in (_request_host(e) for e in chain) if h), "")
            return _failure(msgs.NET_DNS_FAILURE, host)
    for err in chain:
        if isinstance(err, (httpx2.TimeoutException, TimeoutError)):
            host = next((h for h in (_request_host(e) for e in chain) if h), "")
            return _failure(msgs.NET_TIMEOUT, host)
    for err in chain:
        if isinstance(err, (httpx2.ConnectError, ConnectionError)):
            host = next((h for h in (_request_host(e) for e in chain) if h), "")
            return _failure(msgs.NET_CONNECT_FAILED, f"{host} {_one_line(err)}".strip())
    return f"{fset.fg('COLOR_ERROR')}{type(exc).__name__}{fset.fg('COLOR_WARN')}: {_one_line(exc)}"


_DETAIL_WIDTH = 160


def _one_line(err: BaseException) -> str:
    """``err`` as one capped line: a provider error often carries a
    multi-line JSON body, and the failure is one banner line. Whitespace runs,
    newlines included, become one space, so a JSON body still reads."""
    line = " ".join(str(err).split())
    return line if len(line) <= _DETAIL_WIDTH else line[:_DETAIL_WIDTH - 1] + "…"


def _failure(kind: msgs.Message, detail: str) -> str:
    """A failure line: the kind of failure painted hot, then what it names."""
    head = f"{fset.fg('COLOR_ERROR')}{kind.body()}{fset.fg('COLOR_WARN')}"
    return f"{head}: {detail}" if detail else head


def _request_host(err: BaseException) -> str:
    try:
        request = err.request  # httpx2 raises RuntimeError when none is attached
    except Exception:  # noqa: BLE001
        return ""
    return getattr(getattr(request, "url", None), "host", "") or ""
