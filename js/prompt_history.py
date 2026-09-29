"""The prompt history: every line submitted at a REPL prompt, in one file.

`history.file` (unset = ~/.js/state/history.jsonl) holds one JSON object per
line: ts (epoch seconds), cwd, session, agent and text. Every js process
appends to the same file, one whole line per write.

`PromptHistory` is that file as a prompt_toolkit history. It loads the newest
`history.max_entries` entries. With `history.cwd_first` on, Up browses the
entries typed in the current directory first, newest first, then the rest;
off, every entry in time order. A text is offered once, at its first place in
that order. Ctrl-R's incremental search runs over the same loaded entries, so
it finds a prompt typed in any directory, session or agent.

The per-agent history files js kept before (`state/<agent>/history`, in
prompt_toolkit's FileHistory format) are folded into the file the first time
it is loaded after they appear, then removed.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from prompt_toolkit.history import History



@dataclass(frozen=True)
class Entry:
    ts: float
    cwd: str
    session: str
    agent: str
    text: str


@dataclass(frozen=True)
class Origin:
    """Where a prompt is typed: what an entry records besides its text."""

    cwd: str
    session: str
    agent: str


def _entry(record: object) -> Entry | None:
    if not isinstance(record, dict) or not isinstance(record.get("text"), str):
        return None
    ts = record.get("ts")
    return Entry(
        ts=float(ts) if isinstance(ts, (int, float)) else 0.0,
        cwd=str(record.get("cwd") or ""),
        session=str(record.get("session") or ""),
        agent=str(record.get("agent") or ""),
        text=record["text"],
    )


def read_entries(path: Path, limit: int | None = None) -> list[Entry]:
    """The entries in ``path``, oldest first; only the newest ``limit`` when
    it is given. A line that is not an entry is skipped."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return []
    entries: list[Entry] = []
    for line in lines:
        try:
            entry = _entry(json.loads(line))
        except ValueError:
            continue
        if entry is not None:
            entries.append(entry)
    entries.sort(key=lambda entry: entry.ts)
    return entries[-limit:] if limit else entries


def browse_order(entries: Iterable[Entry], cwd: str, *, cwd_first: bool) -> list[str]:
    """The texts of ``entries`` (oldest first) as Up offers them: newest
    first, those typed in ``cwd`` ahead of the rest when ``cwd_first``, each
    text once."""
    # Of two entries with one ts, the later one in the file is the newer.
    newest = sorted(entries, key=lambda entry: entry.ts)[::-1]
    if cwd_first:
        newest = ([entry for entry in newest if entry.cwd == cwd]
                  + [entry for entry in newest if entry.cwd != cwd])
    seen: set[str] = set()
    texts: list[str] = []
    for entry in newest:
        if entry.text not in seen:
            seen.add(entry.text)
            texts.append(entry.text)
    return texts


def _line(entry: Entry) -> bytes:
    return (json.dumps(asdict(entry), ensure_ascii=False) + "\n").encode("utf-8")


def append_entry(path: Path, entry: Entry) -> None:
    """Add ``entry`` to the end of ``path`` in one write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, _line(entry))
    finally:
        os.close(fd)


def _legacy_entries(path: Path, agent: str) -> list[Entry]:
    """The prompts in one FileHistory file: `# <timestamp>` then `+<line>` lines."""
    entries: list[Entry] = []
    ts = 0.0
    lines: list[str] = []

    def flush() -> None:
        if lines:
            entries.append(Entry(ts=ts, cwd="", session="", agent=agent, text="\n".join(lines)))
            lines.clear()

    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if raw.startswith("+"):
            lines.append(raw[1:])
            continue
        flush()
        if raw.startswith("# "):
            with contextlib.suppress(ValueError):
                ts = datetime.fromisoformat(raw[2:].strip()).timestamp()
    flush()
    return entries


def import_legacy(path: Path, state_root: Path) -> int:
    """Fold every `state_root/<agent>/history` file into ``path`` and remove
    it. Returns the number of entries added."""
    legacy = sorted(p for p in state_root.glob("*/history") if p.is_file())
    if not legacy:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    added = 0
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        for old in legacy:
            try:
                entries = _legacy_entries(old, old.parent.name)
            except (FileNotFoundError, UnicodeError):
                continue  # another process folded it first
            if entries:
                os.write(fd, b"".join(_line(entry) for entry in entries))
            added += len(entries)
            with contextlib.suppress(FileNotFoundError):
                old.unlink()
    finally:
        os.close(fd)
    return added


class PromptHistory(History):
    """The prompt history file as the input line's history. ``origin()`` is
    read when an entry is stored and when the history loads."""

    def __init__(self, path: Path, origin: Callable[[], Origin], *,
                 cwd_first: bool = True, limit: int | None = None,
                 state_root: Path | None = None) -> None:
        super().__init__()
        self.path = path
        self._origin = origin
        self._cwd_first = cwd_first
        self._limit = limit
        self._state_root = state_root

    def load_history_strings(self) -> Iterable[str]:
        if self._state_root is not None:
            with contextlib.suppress(OSError):
                import_legacy(self.path, self._state_root)
        entries = read_entries(self.path, self._limit)
        return browse_order(entries, self._origin().cwd, cwd_first=self._cwd_first)

    def store_string(self, string: str) -> None:
        # A prompt is sent whether or not its history line could be written.
        if not string.strip():
            return
        origin = self._origin()
        with contextlib.suppress(OSError):
                append_entry(self.path, Entry(ts=round(time.time(), 3), cwd=origin.cwd,
                                          session=origin.session, agent=origin.agent, text=string))
