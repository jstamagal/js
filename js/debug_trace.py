"""Debug trace autolog. The full request trace (unclipped system prompt + full
tool-schema JSON + the messages sent each call) is far too large for the
terminal, so it is written ONLY to a file: always-on by default under
logs/<agent>/<session>.log, plus any explicit --debug-file. Every IO error
degrades to no logging — the log must never crash a turn."""

from __future__ import annotations

import contextlib
import os
import threading
from pathlib import Path
from typing import Any

import ai
from ai.models.core import params as ai_params

from . import output, paths as _paths, settings, transcript
from .config import Config


class DebugTraceSink:
    """Append-only tee that fans one text stream to N open files (the autolog and
    any --debug-file). Thread-safe. A file that fails a write is dropped and
    its error reported once, so logging never breaks a turn. Stands in for a
    stdout-like stream (write/flush/isatty) so it can also receive the
    redirected turn output."""

    def __init__(self, files: list, paths: list[Path]) -> None:
        self._files = files
        self.paths = paths
        self._lock = threading.Lock()

    def write(self, text: str) -> None:
        self.emit(output.event("debug_text", text=text))

    def emit(self, event: output.OutputEvent) -> None:
        text = dict(event.fields).get("text", "")
        if text:
            self._each_file(lambda f: (f.write(text), f.flush()))

    def flush(self) -> None:
        self._each_file(lambda f: f.flush())

    def _each_file(self, act) -> None:
        """Apply ``act`` to every open file. Failures are reported after the
        lock is released, since the report may be written back into this sink."""
        failures = []
        with self._lock:
            for f in list(self._files):
                try:
                    act(f)
                except Exception as exc:
                    failures.append((str(getattr(f, "name", "")), exc))
                    with contextlib.suppress(Exception):
                        f.close()
                    self._files = [g for g in self._files if g is not f]
        for name, exc in failures:
            output.failed_once(f"debug trace {name}", exc)

    def isatty(self) -> bool:
        return False

    def close(self) -> None:
        with self._lock:
            for f in self._files:
                try:
                    f.close()
                except Exception:
                    pass
            self._files = []


class StdoutTee:
    """Write to the real stdout AND a sink — used by `-d` so the concise trace
    lines and the streamed answer show live on the terminal and also land in the
    log. The oversized request trace bypasses this and goes to the sink only."""

    def __init__(self, primary, sink: DebugTraceSink) -> None:
        self._primary = primary
        self._sink = sink

    def write(self, text: str) -> None:
        try:
            self._primary.write(text)
        except transcript.READER_GONE:
            pass
        self._sink.emit(output.event("debug_text", text=text))

    def write_unlogged(self, text: str) -> None:
        """Terminal-only material (a live region redrawn in place) skips the log."""
        try:
            write = getattr(self._primary, "write_unlogged", self._primary.write)
            write(text)
        except transcript.READER_GONE:
            pass

    def flush(self) -> None:
        try:
            self._primary.flush()
        except transcript.READER_GONE:
            pass
        self._sink.flush()

    def isatty(self) -> bool:
        try:
            return bool(self._primary.isatty())
        except Exception:
            return False


def debug_autolog_path(cfg: Config, live_settings: dict) -> Path | None:
    """Resolve the per-session autolog path, or None when autolog is off. Mirrors
    the session file's name, under logs/<agent>/."""
    if not settings.live_bool_setting(
        live_settings, ("REQUEST_LOG",), settings.knob_attr(cfg, "debug_autolog", "REQUEST_LOG")
    ):
        return None
    session = cfg.session_file
    if not session or session == Path(os.devnull):
        stem = f"{cfg.agent_id}-{os.getpid()}"  # one-shot with no persisted session
    else:
        stem = session.stem
    override = settings.live_optional_str_setting(
        live_settings, ("REQUEST_LOG_DIR",), getattr(cfg, "debug_autolog_dir", None)
    )
    root = Path(override).expanduser() if override else (_paths.logs_root() / cfg.agent_id)
    return root / f"{stem}.log"


def open_debug_trace_sink(paths: list[Path]) -> DebugTraceSink | None:
    """Open each path for append and return a tee sink, or None if none opened."""
    files: list = []
    opened: list[Path] = []
    for p in paths:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            files.append(open(p, "a", encoding="utf-8", errors="backslashreplace"))  # noqa: SIM115 — sink owns the lifetime
            opened.append(p)
        except OSError:
            pass  # a bad log path degrades to no logging, never a crash
    if not files:
        return None
    return DebugTraceSink(files, opened)


def _debug_json(value: Any) -> str:
    import json as _json

    return _json.dumps(value, indent=2, default=str, ensure_ascii=False)


def _message_to_debug(msg: ai.messages.Message) -> Any:
    """Best-effort full serialization of one outgoing message. Never raises."""
    try:
        return msg.model_dump(mode="json")
    except Exception:
        parts = []
        for part in getattr(msg, "parts", None) or []:
            parts.append(
                {k: getattr(part, k) for k in ("text", "tool_name", "tool_args", "tool_call_id")
                 if hasattr(part, k)}
            )
        return {"role": getattr(msg, "role", "?"), "parts": parts}


def _tool_to_debug(tool: ai.types.tools.Tool) -> dict:
    spec = getattr(tool, "spec", None)
    return {
        "name": getattr(tool, "name", "?"),
        "description": getattr(spec, "description", ""),
        "parameters": getattr(spec, "params", {}),
    }


def emit_request_trace(
    *,
    sink: Any,
    model_id: str,
    provider_id: str | None,
    provider_base_url: str | None,
    params: ai_params.InferenceRequestParams | None,
    messages: list[ai.messages.Message],
    tools: list[ai.types.tools.Tool] | None,
    dump_schemas: bool,
    dump_from: int,
) -> None:
    """Byte-honest dump of the request as it leaves js for the SDK: the resolved
    provider/model/params, and — once per turn — the unclipped system prompt and
    FULL tool spec JSON (descriptions and all), then the messages newly sent this
    call. This is the instrument the tool-contract audit reads, so it must not
    clip or summarize. Never raises: a debug trace may not break a turn.

    Written ONLY to ``sink`` (the autolog file / --debug-file), never to stdout —
    this dump is far too large for the terminal. If ``sink`` is None it is a no-op.

    Note: for custom providers this shows the request js hands the SDK,
    including the resolved base_url — the provider's own in-SDK reshaping to its
    wire format happens downstream and is not captured here.
    """
    if sink is None:
        return
    try:
        header = {
            "model_id": model_id,
            "provider_id": provider_id,
            "base_url": provider_base_url or "provider-default",
            "message_count": len(messages),
            "tool_count": len(tools) if tools else 0,
            "tool_names": [tool.name for tool in tools] if tools else [],
        }
        if params is not None:
            try:
                header["params"] = params.model_dump(mode="json")
            except Exception:
                header["params"] = str(params)
        blocks = ["\n━━ REQUEST (model_client) ━━", _debug_json(header)]
        if dump_schemas:
            sys_txt = ""
            if messages and getattr(messages[0], "role", None) == "system":
                first_parts = getattr(messages[0], "parts", None) or []
                sys_txt = getattr(first_parts[0], "text", "") if first_parts else ""
            blocks.append("── SYSTEM PROMPT (unclipped) ──")
            blocks.append(sys_txt)
            blocks.append(f"── TOOL SCHEMAS ({len(tools) if tools else 0}) ──")
            blocks.append(_debug_json([_tool_to_debug(t) for t in (tools or [])]))
        new_msgs = messages[dump_from:] if dump_from else messages
        blocks.append(f"── MESSAGES (+{len(new_msgs)}) ──")
        blocks.append(_debug_json([_message_to_debug(m) for m in new_msgs]))
        sink.write("\n".join(blocks) + "\n")
    except Exception:
        pass
