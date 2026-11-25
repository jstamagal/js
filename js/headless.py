"""JSON output for completed runs and streaming JSON Lines."""

from __future__ import annotations

import contextlib
import io
import json
import re
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

from . import messages as msgs, network_display, snippets, usage as usage_mod
from .toolkit.results import result_text

SCHEMA_VERSION = 2
SUMMARY_CHARS = 200


def tool_result_summary(result: Any) -> dict[str, Any]:
    """`ok`, `bytes`, `lines` and `summary` (the first non-blank line, cut to
    SUMMARY_CHARS characters) of a tool result."""
    text, failed = result_text(result)
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
        self.result: dict[str, Any] = {}
        self.usage = usage_mod.UsageTotals()
        self._lock = threading.Lock()

    def emit(self, kind: str, /, **fields: Any) -> None:
        if kind == "error":
            self.errored = True
        elif kind == "start":
            self.session = fields
        line = json.dumps({**fields, "type": kind}, ensure_ascii=True, default=str)
        with self._lock:
            self.out.write(line + "\n")
            self.out.flush()

    def runtime_event(self, event: str, payload: dict) -> None:
        """The `event_sink` of `runtime.run_turn_async`: each runtime event as
        its JSON event. Events the schema does not name are not written."""
        if event == "stream":
            self.emit("text", delta=payload.get("text", ""))
        elif event == "response":
            self.final_text = payload.get("text", "")
        elif event == "tool_call":
            self.emit("tool_call", id=payload.get("id"), name=payload.get("name"),
                      arguments=_arguments(payload.get("arguments")))
        elif event == "tool_result":
            self.emit("tool_result", id=payload.get("id"), name=payload.get("name"),
                      result=payload.get("result"), **tool_result_summary(payload.get("result")))
        elif event == "usage":
            self.usage.add(usage_mod.CallUsage(**{**{"model": "", "provider": None}, **{key: payload[key] for key in usage_mod.CallUsage.__dataclass_fields__ if key in payload}}))
        elif event == "error":
            self.emit("error", message=payload.get("error"), retryable=bool(payload.get("retryable")))


class OutputEvents:
    """Observe reasoning and retries, which use the presentation event bus."""

    def __init__(self, events: JsonEvents) -> None:
        self.events = events

    def emit(self, event) -> None:
        fields = dict(event.fields)
        if event.name == "reasoning":
            self.events.emit("reasoning", delta=fields.get("text", ""))
        elif event.name == "retry_wait":
            self.events.emit("retry", attempt=fields.get("n"), attempts=fields.get("attempts"),
                             delay=fields.get("delay"), message=str(fields.get("error", "")))


class LastLine:
    """A text stream that passes writes on to `target` and keeps the last
    non-blank line written."""

    def __init__(self, target: TextIO) -> None:
        self.target = target
        self._last_line = ""
        self._pending = ""

    @property
    def last_line(self) -> str:
        return self._pending.strip() or self._last_line

    def write(self, text: str) -> int:
        self._pending += text
        *done, self._pending = self._pending.split("\n")
        for line in done:
            if line.strip():
                self._last_line = line.strip()
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


_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_WARN_MARK = re.compile(r"^\*+\s*")


# The events of the --json run in progress (`json_run`); None outside one.
active: JsonEvents | None = None


def json_run(run: Callable[[], int], *, stream: bool = True) -> int:
    """Keep stdout machine-readable, ending with the completed run object."""
    global active
    stdout = sys.stdout
    events = JsonEvents(stdout if stream else io.StringIO())
    err = LastLine(sys.stderr)
    active = events
    try:
        with contextlib.redirect_stdout(err), contextlib.redirect_stderr(err):
            try:
                code = run()
            except Exception as exc:  # noqa: BLE001 - the stream must still end with a result
                msgs.warn(msgs.FAILED, error=network_display.error_text(exc))
                code = 2 if isinstance(exc, snippets.SnippetError) else 1
    finally:
        active = None
    opened = events.session
    if opened is None:
        events.emit("start", version=SCHEMA_VERSION)
    if code != 0 and not events.errored:
        emit_failure(events, err.last_line, code)
    payload = events.result or {
        "answer": events.final_text if code == 0 else "",
        "session": None if opened is None else opened.get("id"),
        "provider": None if opened is None else opened.get("provider"),
        "model": None if opened is None else opened.get("model"),
        "effort": None if opened is None else opened.get("effort"),
        "usage": None if opened is None else opened.get("usage"),
    }
    if "benchmarks" not in payload:
        if opened is not None and opened.get("file"):
            payload["usage"] = usage_mod.totals(Path(opened["file"])).as_dict()
        payload.update(ok=code == 0, exit_code=code)
    if stream:
        events.emit("end", **payload)
    else:
        print(json.dumps(payload, ensure_ascii=False, default=str), file=stdout)
    return code


def emit_failure(events: JsonEvents, line: str, code: int) -> None:
    """An `error` record of ``line`` without its colour and warning mark,
    after a `start` record when the run wrote none."""
    if events.session is None:
        events.emit("start", version=SCHEMA_VERSION)
    message = _WARN_MARK.sub("", _ANSI.sub("", line))
    events.emit("error", message=message or f"exit {code}", retryable=False)
