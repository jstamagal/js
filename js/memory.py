"""JSONL conversation persistence. Lock-protected, fsync-after-write,
version-tagged. Loader ignores records it doesn't understand."""

from __future__ import annotations

import fcntl
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

SCHEMA_VERSION = 1


@dataclass
class Record:
    kind: Literal["message", "mark"]
    ts: float
    version: int = SCHEMA_VERSION
    message: dict | None = None     # set when kind == "message"
    marker: str | None = None       # set when kind == "mark"

    def to_jsonline(self) -> str:
        return json.dumps(
            {k: v for k, v in self.__dict__.items() if v is not None},
            default=str,
        )

    @classmethod
    def from_dict(cls, d: dict) -> Record | None:
        if d.get("version") != SCHEMA_VERSION:
            return None
        kind = d.get("kind")
        if kind not in {"message", "mark"}:
            return None
        return cls(
            kind=kind,
            ts=float(d.get("ts", time.time())),
            version=SCHEMA_VERSION,
            message=d.get("message"),
            marker=d.get("marker"),
        )


def _open_locked(path: Path, mode: str):
    """Open with appropriate fcntl lock for the mode. Caller must close."""
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, mode)
    lock = fcntl.LOCK_EX if "a" in mode or "w" in mode else fcntl.LOCK_SH
    fcntl.flock(f.fileno(), lock)
    return f


def _heal_orphaned_tool_calls(messages: list[dict]) -> list[dict]:
    """Backfill tool results lost to crashes or early-exit bugs so every assistant
    ``tool_calls`` message is followed by one tool message per ``tool_call_id`` —
    a hard requirement of OpenAI-shape providers (DeepSeek rejects the whole
    request otherwise). Synthetic results are explicit about the loss."""
    healed: list[dict] = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        healed.append(msg)
        i += 1
        calls = msg.get("tool_calls") if msg.get("role") == "assistant" else None
        if not calls:
            continue
        answered: set[str] = set()
        while i < len(messages) and messages[i].get("role") == "tool":
            answered.add(messages[i].get("tool_call_id"))
            healed.append(messages[i])
            i += 1
        for call in calls:
            cid = call.get("id")
            if cid and cid not in answered:
                healed.append({
                    "role": "tool",
                    "tool_call_id": cid,
                    "name": (call.get("function") or {}).get("name", ""),
                    "content": "ERROR: tool result was not recorded (session interrupted)",
                })
    return healed


def balance_orphaned_tool_calls(messages: list[dict]) -> list[dict]:
    """Public entry to the orphan-tool-call backfill, for the live REPL to repair
    an interrupted turn's tail in memory before the next turn (the on-load path in
    `load_messages` already heals the persisted copy)."""
    return _heal_orphaned_tool_calls(messages)


def _strip_orphan_reasoning(messages: list[dict]) -> list[dict]:
    """Drop ``reasoning_content`` from assistant messages that have NO ``tool_calls``
    when rebuilding the in-memory conversation. The on-disk JSONL keeps the field
    (archive value, costs nothing on disk); only the live conversation that gets
    sent back to the provider is trimmed. DeepSeek IGNORES AND BILLS the field on
    tool-free assistant turns — roughly 500 wasted prompt tokens each — but still
    REQUIRES it on the assistant turn that carried tool_calls. Mirrors the
    write-side rule in ``js/runtime.py``.
    """
    out: list[dict] = []
    for msg in messages:
        if msg.get("role") == "assistant" and "reasoning_content" in msg and not msg.get("tool_calls"):
            cleaned = {k: v for k, v in msg.items() if k != "reasoning_content"}
            out.append(cleaned)
        else:
            out.append(msg)
    return out



def _compaction_summary_message(summary: str) -> dict:
    return {"role": "user", "content": f"<compaction-summary>\n{summary}\n</compaction-summary>"}


def _parse_compaction_marker(marker: str) -> dict | None:
    if not marker.startswith("compaction:"):
        return None
    try:
        data = json.loads(marker.split(":", 1)[1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("summary"), str):
        return None
    return data

def load_messages(memory_file: Path) -> list[dict]:
    """Return the OpenAI-shape message list from disk, honoring control marks."""
    if not memory_file.exists():
        return []
    messages: list[dict] = []
    with _open_locked(memory_file, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = Record.from_dict(json.loads(line))
            except json.JSONDecodeError:
                continue
            if rec is None:
                continue
            if rec.kind == "mark":
                if rec.marker == "session_reset":
                    messages.clear()
                elif rec.marker and rec.marker.startswith("rollback_to:"):
                    try:
                        keep = max(0, int(rec.marker.split(":", 1)[1]))
                    except ValueError:
                        continue
                    del messages[keep:]
                elif rec.marker:
                    data = _parse_compaction_marker(rec.marker)
                    if data is not None:
                        keep_from = int(data.get("keep_from", len(messages)))
                        keep_from = max(0, min(keep_from, len(messages)))
                        messages[:] = [_compaction_summary_message(data["summary"]), *messages[keep_from:]]
                continue
            if rec.kind != "message" or rec.message is None:
                continue
            if rec.message.get("role") in {"user", "assistant", "tool", "system"}:
                messages.append(rec.message)
    return _strip_orphan_reasoning(_heal_orphaned_tool_calls(messages))


def _append(memory_file: Path, rec: Record) -> None:
    with _open_locked(memory_file, "a") as f:
        f.write(rec.to_jsonline() + "\n")
        f.flush()
        os.fsync(f.fileno())


def append_message(memory_file: Path, message: dict) -> None:
    _append(memory_file, Record(kind="message", ts=time.time(), message=message))


def append_mark(memory_file: Path, marker: str) -> None:
    _append(memory_file, Record(kind="mark", ts=time.time(), marker=marker))


def append_compaction_mark(memory_file: Path, *, summary: str, keep_from: int, forced: bool = False) -> None:
    payload = {"summary": summary, "keep_from": int(keep_from), "forced": bool(forced)}
    append_mark(memory_file, "compaction:" + json.dumps(payload, separators=(",", ":")))


def wipe(memory_file: Path) -> Path | None:
    """Rotate the memory file to a .bak suffix. Returns the .bak path or None."""
    if not memory_file.exists():
        return None
    bak = memory_file.with_suffix(memory_file.suffix + ".bak")
    if bak.exists():
        idx = 1
        while True:
            candidate = memory_file.with_suffix(memory_file.suffix + f".bak.{idx}")
            if not candidate.exists():
                bak = candidate
                break
            idx += 1
    memory_file.rename(bak)
    return bak
