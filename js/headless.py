"""JSON output for completed runs and streaming JSON Lines."""

from __future__ import annotations

import json
import threading
from typing import Any, TextIO

from .toolkit.results import summary_text
from . import usage as usage_mod

SCHEMA_VERSION = 2
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

