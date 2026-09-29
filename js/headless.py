"""`js -p --json`: the run as JSON events, one object per line on stdout.

The schema is in docs/headless-json.md. Every event has a `type`; the first
is `session` and the last is `result`. Everything else the run prints goes to
stderr, so stdout carries only these lines.
"""

from __future__ import annotations

import json
import threading
from typing import Any, TextIO

from .toolkit.core import ToolResult

SCHEMA_VERSION = 1
SUMMARY_CHARS = 200


def _result_text(result: Any) -> tuple[str, bool]:
    """A tool result's text and whether it reports an error."""
    if isinstance(result, ToolResult):
        text = result.dehydrated()
        return text, result.is_error or text.startswith("ERROR")
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
    return text, text.startswith("ERROR")


def tool_result_summary(result: Any) -> dict[str, Any]:
    """`ok`, `bytes`, `lines` and `summary` (the first non-blank line, cut to
    SUMMARY_CHARS characters) of a tool result."""
    text, failed = _result_text(result)
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
        line = json.dumps({"type": kind, **fields}, ensure_ascii=False, default=str)
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
