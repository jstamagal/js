"""Durable, correlated records of every compaction attempt."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import asdict, is_dataclass
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import traceback
from uuid import uuid4

from . import paths

ACTIVE_FLIGHT: ContextVar[CompactionFlight | None] = ContextVar("compaction_flight", default=None)


def _json(value):
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _redact(value):
    if isinstance(value, dict):
        return {key: "[redacted]" if any(word in key.lower() for word in
                ("api_key", "authorization", "password", "secret", "access_token", "refresh_token"))
                else _redact(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


class CompactionFlight:
    def __init__(self, cfg, system, messages, *, trigger, forced, focus, preserve_from, details):
        self.id = uuid4().hex
        self.started = time.monotonic()
        self.cfg = cfg
        settings = getattr(cfg, "settings", {}) or {}
        compact = settings.get("compact", {})
        override = compact.get("flight_log_dir")
        directory = Path(override).expanduser() if override else paths.logs_root() / cfg.agent_id / "compactions"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / f"{cfg.session_file.stem}-{self.id}.jsonl"
        self.stream = os.fdopen(os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8")
        self.record("start", model=cfg.model, provider=cfg.provider_id,
                    base_url=cfg.provider_base_url, session=str(cfg.session_file),
                    pid=os.getpid(), parent_pid=os.getppid(),
                    trigger=trigger, forced=forced, focus=focus, preserve_from=preserve_from,
                    settings=_redact(settings), max_output_tokens=cfg.max_output_tokens,
                    model_context_window=getattr(cfg, "model_context_window", None),
                    caller_stack=traceback.format_stack(), details=details,
                    disk_compaction_source_sha256=hashlib.sha256(Path(__file__).with_name("compaction.py").read_bytes()).hexdigest())
        self.snapshot("before", system, messages)

    def record(self, event, **payload):
        self.stream.write(json.dumps({"version": 1, "id": self.id, "event": event,
                                      "ts": time.time(), **payload}, ensure_ascii=False, default=_json) + "\n")
        self.stream.flush()
        os.fsync(self.stream.fileno())
        if event in {"start", "success", "failure", "cancelled", "skipped"}:
            settings = getattr(self.cfg, "settings", {}) or {}
            runtime = settings.get("runtime", {})
            directory = runtime.get("debug_autolog_dir")
            directory = Path(directory).expanduser() if directory else paths.logs_root() / self.cfg.agent_id
            if runtime.get("debug_autolog", True):
                try:
                    directory.mkdir(parents=True, exist_ok=True)
                    with (directory / f"{self.cfg.session_file.stem}.log").open("a", encoding="utf-8") as log:
                        log.write("FLIGHT " + json.dumps({"ts": time.time(), "kind": "compaction_" + event,
                                  "attempt_id": self.id, "flight_path": str(self.path),
                                  "model": self.cfg.model, "session": str(self.cfg.session_file)}, default=_json) + "\n")
                except OSError as exc:
                    print(f"[FLIGHT AUTOLOG ERROR] {exc} flight={self.path}", file=sys.stderr, flush=True)

    def snapshot(self, phase, system, messages):
        encoded = json.dumps({"system": system, "messages": messages}, ensure_ascii=False, default=_json)
        self.record(phase, system=system, messages=messages, message_count=len(messages),
                    utf8_bytes=len(encoded.encode()), sha256=hashlib.sha256(encoded.encode()).hexdigest())

    def notice(self, event, detail=""):
        print(f"[COMPACT {event.upper()} {self.id[:12]}] {detail} flight={self.path}", file=sys.stderr, flush=True)

    def write(self, text):
        if text:
            self.record("summary_trace", text=text)

    def flush(self):
        self.stream.flush()

    def finish(self, event, system, messages, **payload):
        self.snapshot("after", system, messages)
        self.record(event, elapsed_s=time.monotonic() - self.started, **payload)
        self.notice(event, payload.get("error") or payload.get("result", ""))

    def close(self):
        self.stream.close()
