"""`js -p --json`: the run as JSON events, one object per line on stdout.

The schema is in docs/headless-json.md. Every event has a `type`; the first
is `session` and the last is `result`. Everything else the run prints goes to
stderr, so stdout carries only these lines.
"""

from __future__ import annotations

import contextlib
import json
import math
import sys
import threading
import time
from collections.abc import Iterator
from typing import Any, TextIO

from . import messages as msgs
from . import activity
from .toolkit.core import TurnStatus
from .toolkit.results import summary_text

SCHEMA_VERSION = 1
SUMMARY_CHARS = 200


def tool_result_summary(result: Any) -> dict[str, Any]:
    """`ok`, `bytes`, `lines` and `summary` (the first non-blank line, cut to
    SUMMARY_CHARS characters) of a tool result."""
    text, failed = summary_text(result)
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if len(first) > SUMMARY_CHARS:
        first = first[:SUMMARY_CHARS - 1] + "…"
    return {
        "ok": not failed,
        "bytes": len(text.encode("utf-8", errors="replace")),
        "lines": len(text.splitlines()),
        "summary": first,
    }


def _arguments(raw: Any) -> Any:
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


class JsonEvents:
    """Writes the events of one headless run to `out`."""

    def __init__(self, out: TextIO) -> None:
        self.out = out
        self.errored = False
        self.final_text = ""
        self.session: dict[str, Any] | None = None
        self._lock = threading.Lock()

    def emit(self, kind: str, **fields: Any) -> None:
        if kind == "error":
            self.errored = True
        elif kind == "session":
            self.session = fields
        line = json.dumps({"type": kind, **fields}, ensure_ascii=True, default=str)
        with self._lock:
            self.out.write(line + "\n")
            self.out.flush()

    def runtime_event(self, event: str, payload: dict) -> None:
        """The `event_sink` of `runtime.run_turn_async`: each runtime event as
        its JSON event. Events the schema does not name are not written."""
        if event == "turn_start":
            self.emit("turn_start", model=payload.get("model"), provider=payload.get("provider_id"))
        elif event == "stream":
            self.emit("text", delta=payload.get("text", ""))
        elif event == "response":
            text = payload.get("text", "")
            self.final_text = text
            fields = {"text": text, "finish_reason": payload.get("finish_reason")}
            if payload.get("incomplete_reason"):
                fields["incomplete_reason"] = payload["incomplete_reason"]
            self.emit("message", **fields)
        elif event == "tool_call":
            self.emit("tool_call", id=payload.get("id"), name=payload.get("name"),
                      arguments=_arguments(payload.get("arguments")))
        elif event == "tool_result":
            self.emit("tool_result", id=payload.get("id"), name=payload.get("name"),
                      **tool_result_summary(payload.get("result")))
        elif event == "usage":
            self.emit("usage", **payload)
        elif event == "error":
            self.emit("error", message=payload.get("error"), retryable=bool(payload.get("retryable")))
        elif event == "turn_end":
            fields = {"reason": payload.get("reason"), "usage": payload.get("usage")}
            for key in ("finish_reason", "incomplete_reason"):
                if payload.get(key):
                    fields[key] = payload[key]
            self.emit("turn_end", **fields)


class LastLine:
    """A text stream that passes writes on to `target` and keeps the last
    non-blank line written."""

    def __init__(self, target: TextIO) -> None:
        self.target = target
        self.last_line = ""
        self._pending = ""

    def write(self, text: str) -> int:
        self._pending += text
        *done, self._pending = self._pending.split("\n")
        for line in done:
            if line.strip():
                self.last_line = line.strip()
        return self.target.write(text)

    def flush(self) -> None:
        self.target.flush()

    def isatty(self) -> bool:
        try:
            return bool(self.target.isatty())
        except (AttributeError, ValueError, OSError):
            return False

    def fileno(self) -> int:
        return self.target.fileno()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.target, name)


def _elapsed(seconds: float) -> str:
    whole = int(seconds)
    return f"{whole}s" if whole < 60 else f"{whole // 60}m {whole % 60:02d}s"


# `time.monotonic()` of the last write through a `watch_output` stream.
_last_output = 0.0


class _Clocked:
    """A stream that stamps `_last_output` on every write."""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream

    def write(self, text: str) -> int:
        global _last_output
        if text:
            _last_output = time.monotonic()
        return self._stream.write(text)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


@contextlib.contextmanager
def watch_output() -> Iterator[None]:
    """Put a `_Clocked` stream in front of sys.stdout and sys.stderr. The
    headless entry point does this first, so every tee, trace sink and
    display that later takes either stream writes through it."""
    stdout, stderr = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = _Clocked(stdout), _Clocked(stderr)
    try:
        yield
    finally:
        sys.stdout, sys.stderr = stdout, stderr


def last_output() -> float:
    """When anything was last written to a watched stdout or stderr."""
    return _last_output


class Progress:
    """`js -p`: a stderr line once PROGRESS_INTERVAL seconds pass with nothing
    written to stdout or stderr (`watch_output`), naming what runs now (a
    tool call, a model call, a wait before a model call is sent again) and for
    how long, and again after each further interval of silence. Silence
    counts across one call ending and the next starting."""

    def __init__(self, status: TurnStatus, interval: float, *, out: TextIO | None = None) -> None:
        self.status = status
        self.interval = interval
        self.out = out if out is not None else sys.stderr
        self._since = time.monotonic()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def line(self, now: float | None = None) -> str | None:
        """What runs now as a progress line, or None when nothing does."""
        now = time.monotonic() if now is None else now
        running = activity.running()
        tools = [item for item in running if item.kind == activity.TOOL]
        if tools:
            elapsed = _elapsed(now - tools[0].started)
            if len(tools) > 1:
                return msgs.PROGRESS_TOOLS.line(tool=tools[0].name, extra=len(tools) - 1, elapsed=elapsed)
            return msgs.PROGRESS_TOOL.line(tool=tools[0].name, elapsed=elapsed)
        waits = [item for item in running if item.kind == activity.RETRY]
        if waits:
            left = max(0, math.ceil(waits[0].started + waits[0].seconds - now))
            return msgs.PROGRESS_RETRY.line(seconds=left)
        if running:
            return msgs.PROGRESS_MODEL.line(elapsed=_elapsed(now - running[0].started),
                                            tokens=self.status.output_tokens)
        return None

    def tick(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        if now - max(self._since, last_output()) < self.interval:
            return
        text = self.line(now)
        if text is not None:
            self.out.write(text + "\n")
            self.out.flush()
            self._since = now

    def _run(self) -> None:
        while not self._stop.wait(min(self.interval, 1.0)):
            self.tick()

    def __enter__(self) -> Progress:
        if self.interval > 0:
            self._thread = threading.Thread(target=self._run, name="js-progress", daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
