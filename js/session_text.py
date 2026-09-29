"""The readable `.txt` transcript beside each session `.jsonl`.

The `.jsonl` is the record; the `.txt` is rebuilt from it after every write, so
the two stay in step. Its shape is fixed so ripgrep and head work on it:

    agent: defaultagent   dir: /home/ronald_rump/js   mode: repl
    models: deepseek-v4-flash → xiaomi/mimo-v2.6-pro (#0031)
    started: 2026-09-29 08:02   last: 2026-09-29 11:40   turns: 41
    branched-from: -
    tags: js · linux admin

    #0001 08:02 you  APE
    #0015 08:31 tool:shell  look at the spill file  $ wc -lc result.txt  → exit 0, 91984B
    #0016 08:32 ape  the file has no real newlines

Five header lines and a blank line, then one line per message, numbered by the
message's position among the message records of the `.jsonl`; a multi-line
message continues on indented lines. An assistant message that calls tools is
not a line of its own: its text labels each of its calls, and each tool result
is one line with the tool's name, that label, the call's first line, and the
result's exit code and size. Tool output is only in the `.jsonl`, at the same
message number. A message a later rollback took back out of the conversation
(an aborted turn, results cleared by compaction and written again) is not
shown; compaction itself removes nothing from the `.txt`. The `tags:` line
shows the newest `tags` record (`js.session_tags`).

Each file's parse is kept in memory between writes, so a write reads only the
records appended since the last one.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import session_store

USER = "you"
ASSISTANT = "ape"
_INDENT = " " * 12
_EXIT = re.compile(r"^exit=(-?\d+)$", re.MULTILINE)
_ARG_WIDTH = 200
_CACHED_FILES = 64


def _clock(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M") if ts is not None else "--:--"


def _day(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts is not None else "-"


def _lines(text: str) -> list[str]:
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return [line for line in lines if line.strip()]


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return "" if content is None else str(content)
    parts = []
    for part in content:
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            parts.append(part["text"])
        elif isinstance(part, dict) and part.get("type"):
            parts.append(f"[{part['type']}]")
        elif isinstance(part, str):
            parts.append(part)
    return "\n".join(parts)


def _call_line(arguments: Any) -> str:
    """The first line of a call: `$ command` for a command, else its scalar arguments."""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            return (_lines(arguments) or [""])[0][:_ARG_WIDTH]
    if not isinstance(arguments, dict):
        return ""
    command = arguments.get("command")
    if isinstance(command, str) and command.strip():
        return "$ " + _lines(command)[0]
    pairs = []
    for key, value in arguments.items():
        if isinstance(value, (str, int, float, bool)) and str(value).strip():
            pairs.append(f"{key}={(_lines(str(value)) or [''])[0]}")
        elif isinstance(value, list) and value and isinstance(value[0], str):
            pairs.append(f"{key}={(_lines(value[0]) or [''])[0]}")
    line = " ".join(pairs)
    return line if len(line) <= _ARG_WIDTH else line[:_ARG_WIDTH - 1] + "…"


def _outcome(content: Any) -> str:
    text = _content_text(content)
    size = f"{len(text.encode('utf-8'))}B"
    code = _EXIT.search("\n".join(text.split("\n", 6)[:6]))
    if code is not None:
        return f"exit {code.group(1)}, {size}"
    if text.lstrip().startswith("ERROR"):
        return f"error, {size}"
    return size


@dataclass
class _Call:
    name: str
    label: str
    line: str
    rest: list[str] = field(default_factory=list)
    model: str | None = None


@dataclass
class Row:
    """One message as the transcript shows it.

    `lines` is a user, assistant or system message's text, one entry per
    non-blank line. A tool row is the result of one call: `label` is the first
    line of the text the model wrote alongside the call, `lines` the rest of
    that text, `call` the call's first line and `outcome` its exit and size.
    `model` is the stamp of the assistant message that wrote the row."""

    number: int
    role: str
    ts: float | None
    who: str
    lines: list[str] = field(default_factory=list)
    label: str = ""
    call: str = ""
    outcome: str = ""
    model: str | None = None
    # The message record's id: what a branch at this row splits at.
    id: str | None = None

    def first(self) -> str:
        """The row's first line in the `.txt`."""
        if self.role != "tool":
            return self.lines[0] if self.lines else ""
        parts = [part for part in (self.label, self.call) if part]
        parts.append(f"→ {self.outcome}")
        return "  ".join(parts)

    def rest(self) -> list[str]:
        return self.lines if self.role == "tool" else self.lines[1:]

    def txt(self) -> list[str]:
        head = f"#{self.number:04d} {_clock(self.ts)} {self.who}  {self.first()}".rstrip()
        return [head, *(_INDENT + line for line in self.rest())]

    def heading(self) -> str:
        """What a list of messages shows for the row: the text's first line;
        for a tool row the model's accompanying text, else the call's first line."""
        if self.role == "tool":
            return self.label or self.call
        return self.lines[0] if self.lines else ""

    def searchable(self) -> str:
        """The row's text for search, on one line: what the operator and the
        model wrote, and for a tool row its label and call. Never tool output."""
        if self.role == "tool":
            words = [self.label, *self.lines, self.call]
        elif self.role in ("user", "assistant"):
            words = self.lines
        else:
            return ""
        return " ".join(word for word in words if word)


@dataclass
class _Transcript:
    agent: str | None = None
    last_agent: str | None = None
    cwd: str | None = None
    mode: str | None = None
    command: list[str] | None = None
    parent: str | None = None
    branch: dict | None = None
    title: str | None = None
    first_model: str | None = None
    last_stamp: dict | None = None
    models: list[tuple[str, int]] = field(default_factory=list)
    first_ts: float | None = None
    started_ts: float | None = None
    last_ts: float | None = None
    count: int = 0
    # Message number -> row, for the messages the conversation still holds.
    entries: dict[int, Row] = field(default_factory=dict)
    # The replayed history as `js.memory` rebuilds it, one light dict per
    # message; `_entry` is its message number. Rollbacks cut it the way they
    # cut the replay, and the entries they cut are superseded.
    replay: list[dict] = field(default_factory=list)
    calls: dict[str, _Call] = field(default_factory=dict)
    # Record id -> message number, for naming a branch point by its number.
    numbers: dict[str, int] = field(default_factory=dict)
    # The newest `tags` record (`js.session_tags`), or None.
    tagging: dict | None = None

    def feed(self, record: Any) -> None:
        if not isinstance(record, dict):
            return
        if record.get("kind") == "tags":
            # Written after the session ends, so its time is not the session's.
            self.tagging = record
            return
        ts = record.get("ts")
        if isinstance(ts, (int, float)):
            self.first_ts = ts if self.first_ts is None else min(self.first_ts, ts)
            self.last_ts = ts if self.last_ts is None else max(self.last_ts, ts)
        else:
            ts = None
        kind = record.get("kind")
        if kind == "session_metadata":
            self._metadata(record)
        elif kind == "message":
            self._message(record.get("message"), ts, record.get("stamp"), replayed=True)
            self._number(record)
            self._identify(record)
        elif kind == "mark" and isinstance(record.get("marker"), str):
            self._mark(record["marker"])
        elif kind == "title" and isinstance(record.get("title"), str):
            self.title = record["title"]
        elif kind is None and record.get("role") in {"user", "assistant", "tool", "system"}:
            # Sessions predating the record envelope stored bare messages.
            self._message(record, None, None, replayed=False)
            self._number(record)
            self._identify(record)

    def _metadata(self, record: dict) -> None:
        if self.started_ts is None and isinstance(record.get("ts"), (int, float)):
            self.started_ts = record["ts"]
        if isinstance(record.get("agent"), str):
            self.last_agent = record["agent"]
            if self.agent is None:
                self.agent = record["agent"]
        if self.cwd is None and isinstance(record.get("cwd"), str):
            self.cwd = record["cwd"]
        if self.mode is None and isinstance(record.get("mode"), str):
            self.mode = record["mode"]
        if self.command is None and isinstance(record.get("command"), list):
            self.command = [str(part) for part in record["command"]]
        if self.parent is None and isinstance(record.get("parent_session"), str):
            self.parent = record["parent_session"]
        if isinstance(record.get("model"), str) and record["model"]:
            self.last_stamp = {"model": record["model"]}
            if self.first_model is None:
                self.first_model = record["model"]
        if self.branch is None and session_store.branch_parent(record) is not None:
            self.branch = record

    def _branch_point(self) -> tuple[str, int | None] | None:
        """The parent session of a branch and the number of the message it
        split at, as this file numbers it."""
        parent = session_store.branch_parent(self.branch) if self.branch is not None else None
        if parent is None:
            return None
        session, point = parent
        number = point if isinstance(point, int) else self.numbers.get(point) if isinstance(point, str) else None
        return session, number

    def _branched(self) -> str | None:
        parent = self._branch_point()
        if parent is None:
            return None
        session, number = parent
        label = session_store.display_name(Path(session))
        return f"{label} {session_store.point_label(number)}" if number is not None else label

    def _number(self, record: dict) -> None:
        if isinstance(record.get("id"), str):
            self.numbers[record["id"]] = self.count

    def _identify(self, record: dict) -> None:
        row = self.entries.get(self.count)
        if row is not None and isinstance(record.get("id"), str):
            row.id = record["id"]

    def _mark(self, marker: str) -> None:
        from . import memory

        if marker == "session_reset":
            self.replay.clear()
            return
        if marker.startswith("rollback_to:"):
            try:
                keep = max(0, int(marker.split(":", 1)[1]))
            except ValueError:
                return
            self.replay[:] = memory.balance_orphaned_tool_calls(self.replay)
            for light in self.replay[keep:]:
                self.entries.pop(light.get("_entry"), None)
            del self.replay[keep:]
            return
        compaction = memory._parse_compaction_marker(marker)
        if compaction is not None:
            self.replay[:] = memory.balance_orphaned_tool_calls(self.replay)
            keep_from = max(0, min(int(compaction.get("keep_from", len(self.replay))), len(self.replay)))
            synthetic = [{"role": "user"}] * (2 if compaction.get("rehydrated") else 1)
            self.replay[:] = [*synthetic, *self.replay[keep_from:]]

    def _message(self, message: Any, ts: float | None, stamp: Any, *, replayed: bool) -> None:
        if not isinstance(message, dict):
            return
        self.count += 1
        number = self.count
        role = message.get("role")
        if replayed:
            light = {"role": role, "_entry": number}
            if role == "assistant" and message.get("tool_calls"):
                light["tool_calls"] = [
                    {"id": call.get("id"), "function": {"name": (call.get("function") or {}).get("name", "")}}
                    for call in message["tool_calls"] if isinstance(call, dict)]
            if role == "tool":
                light["tool_call_id"] = message.get("tool_call_id")
            self.replay.append(light)
        if role == "user":
            lines = _lines(_content_text(message.get("content"))) or [""]
            self.entries[number] = Row(number, role, ts, USER, lines)
        elif role == "assistant":
            model = self._stamp(stamp, number)
            lines = _lines(_content_text(message.get("content")))
            calls = message.get("tool_calls") or []
            if not calls:
                if lines:
                    self.entries[number] = Row(number, role, ts, ASSISTANT, lines, model=model)
                return
            for index, call in enumerate(calls):
                if not isinstance(call, dict):
                    continue
                function = call.get("function") or {}
                self.calls[str(call.get("id"))] = _Call(
                    str(function.get("name") or "?"),
                    lines[0] if lines else "",
                    _call_line(function.get("arguments")),
                    lines[1:] if index == 0 else [],
                    model,
                )
        elif role == "tool":
            call = self.calls.pop(str(message.get("tool_call_id")), None)
            name = call.name if call else str(message.get("name") or "?")
            self.entries[number] = Row(
                number, role, ts, f"tool:{name}", call.rest if call else [],
                label=call.label if call else "", call=call.line if call else "",
                outcome=_outcome(message.get("content")), model=call.model if call else None)
        elif role == "system":
            lines = _lines(_content_text(message.get("content"))) or [""]
            self.entries[number] = Row(number, role, ts, "system", lines)

    def _stamp(self, stamp: Any, number: int) -> str | None:
        model = stamp.get("model") if isinstance(stamp, dict) else None
        if not isinstance(model, str) or not model:
            return None
        self.last_stamp = dict(stamp)
        if not self.models or self.models[-1][0] != model:
            self.models.append((model, number))
        return model

    def rows(self) -> list[Row]:
        return [self.entries[number] for number in sorted(self.entries)]

    def turns(self) -> int:
        return sum(1 for row in self.entries.values() if row.role == "user")

    def header(self) -> list[str]:
        if self.models:
            chain = [self.models[0][0], *(f"{model} (#{number:04d})" for model, number in self.models[1:])]
            models = " → ".join(chain)
        else:
            models = self.first_model or "-"
        started = self.started_ts if self.started_ts is not None else self.first_ts
        return [
            f"agent: {self.agent or '-'}   dir: {self.cwd or '-'}   mode: {self.mode or '-'}",
            f"models: {models}",
            f"started: {_day(started)}   last: {_day(self.last_ts)}   turns: {self.turns()}",
            f"branched-from: {self._branched() or '-'}",
            f"tags: {' · '.join(self.tags()) or '-'}",
            "",
        ]

    def tags(self) -> list[str]:
        """The tags of the newest `tags` record."""
        tags = (self.tagging or {}).get("tags")
        return [str(tag) for tag in tags] if isinstance(tags, list) else []

    def text(self) -> str:
        rows = [line for row in self.rows() for line in row.txt()]
        return "\n".join([*self.header(), *rows]) + "\n"

    def summary(self) -> dict[str, Any]:
        """What the session catalog keeps for the session: where and how it
        started, its size in turns and calls, its model stamps, and its tags
        with the tag list and message count they were judged at."""
        rows = self.rows()
        final = next((row for row in reversed(rows) if row.role == "assistant"), None)
        parent = self._branch_point()
        models = [model for model, _ in self.models] or ([self.first_model] if self.first_model else [])
        return {
            "agent": self.last_agent or self.agent,
            "cwd": self.cwd,
            "mode": self.mode,
            "command": self.command,
            "title": self.title,
            "parent": self.parent,
            "branch_of": parent[0] if parent else None,
            "branch_point": parent[1] if parent else None,
            "started": self.started_ts if self.started_ts is not None else self.first_ts,
            "last": self.last_ts,
            "turns": self.turns(),
            "messages": len(rows),
            "tool_calls": sum(1 for row in rows if row.role == "tool"),
            "replied": any(row.role in ("assistant", "tool") for row in rows),
            "final_len": len("\n".join(final.lines)) if final is not None else 0,
            "models": models,
            "model_changes": [[model, number] for model, number in self.models],
            "last_stamp": self.last_stamp,
            "tags": self.tags(),
            "tags_list": (self.tagging or {}).get("list"),
            "tags_through": (self.tagging or {}).get("through"),
        }

    def search_lines(self) -> list[tuple[int, str, float | None, str]]:
        """(number, who, ts, text) for every row search reads, in order; the
        `/name` title first, as number 0."""
        lines: list[tuple[int, str, float | None, str]] = [(0, "name", None, self.title)] if self.title else []
        for row in self.rows():
            text = row.searchable()
            if text:
                lines.append((row.number, row.who, row.ts, text))
        return lines


def parse(session_file: Path) -> _Transcript:
    """The whole of `session_file`, parsed from the start."""
    transcript = _Transcript()
    with open(session_file, encoding="utf-8", errors="replace") as stream:
        for line in stream:
            _feed_line(transcript, line)
    return transcript


def rows(session_file: Path) -> list[Row]:
    """The rows of `session_file` the conversation still holds, in order."""
    return parse(session_file).rows()


def render(session_file: Path) -> str:
    """The whole transcript of `session_file`, parsed from the start."""
    return parse(session_file).text()


def _feed_line(transcript: _Transcript, line: str) -> None:
    line = line.strip()
    if not line:
        return
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return
    transcript.feed(record)


@dataclass
class _Parsed:
    device: int
    inode: int
    offset: int
    transcript: _Transcript


_lock = threading.Lock()
_parsed: OrderedDict[str, _Parsed] = OrderedDict()


def _write(path: Path, text: str) -> None:
    staging = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        staging.write_text(text, encoding="utf-8")
        os.replace(staging, path)
    except OSError:
        try:
            staging.unlink()
        except OSError:
            pass


def refresh(session_file: Path) -> None:
    """Bring the `.txt` beside `session_file` up to date with it.

    Only a `.jsonl` gets one. A failure to read or write leaves the `.txt` as
    it was: it is derived, and the record is what matters."""
    session_file = Path(session_file)
    if session_file.suffix != session_store.SUFFIX:
        return
    key = str(session_file.resolve(strict=False))
    with _lock:
        try:
            info = os.stat(session_file)
            parsed = _parsed.pop(key, None)
            if (parsed is None or (parsed.device, parsed.inode) != (info.st_dev, info.st_ino)
                    or info.st_size < parsed.offset):
                parsed = _Parsed(info.st_dev, info.st_ino, 0, _Transcript())
            with open(session_file, "rb") as stream:
                stream.seek(parsed.offset)
                data = stream.read()
        except OSError:
            return
        end = data.rfind(b"\n") + 1
        for line in data[:end].decode("utf-8", errors="replace").splitlines():
            _feed_line(parsed.transcript, line)
        parsed.offset += end
        _parsed[key] = parsed
        while len(_parsed) > _CACHED_FILES:
            _parsed.popitem(last=False)
        _write(session_store.text_path(session_file), parsed.transcript.text())
        _index(session_file, parsed.transcript, size=parsed.offset, mtime_ns=info.st_mtime_ns)


def _index(session_file: Path, transcript: _Transcript, *, size: int, mtime_ns: int) -> None:
    """Keep the session's catalog entry and search text in step with its `.txt`.
    The index is derived; a failure leaves it for the next catalog open."""
    from . import session_index

    try:
        session_index.store(session_file, transcript.summary(), transcript.search_lines(),
                            size=size, mtime_ns=mtime_ns)
    except (OSError, sqlite3.Error):
        pass


def forget(session_file: Path) -> None:
    """Drop the `.txt` and the index entry of a session file that no longer
    exists as such."""
    from . import session_index

    session_file = Path(session_file)
    with _lock:
        _parsed.pop(str(session_file.resolve(strict=False)), None)
    try:
        session_store.text_path(session_file).unlink()
    except OSError:
        pass
    try:
        session_index.forget(session_file)
    except (OSError, sqlite3.Error):
        pass
