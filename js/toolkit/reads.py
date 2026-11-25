"""The read ledger: what of each file the model has seen, so an edit can be
refused when it touches lines the model never read, or content that changed
since it read it.

Coverage is recorded per tool call, so the runtime can narrow one call's
share after it clips that call's result, and the line reads delivered whole
are remembered so a repeated read can name the earlier one instead.
"""

from __future__ import annotations

import contextvars
import hashlib
import itertools
import json
import os
import secrets
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .core import ToolContext


# The id of the tool call running in this thread or task. Read coverage is
# recorded per call, so the runtime can narrow one call's coverage after it
# clips that call's result.
CALL_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar("js_tool_call_id", default=None)
# False while the running call's result goes to code (a kernel cell) rather
# than to the model's history.
FOR_MODEL: contextvars.ContextVar[bool] = contextvars.ContextVar("js_tool_call_for_model", default=True)
_anonymous_calls = itertools.count()

# Upper bound on the bytes remember_content keeps; the oldest entries go first.
_KNOWN_CONTENT_MAX_BYTES = 64 * 1024 * 1024


def atomic_write(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` through a temporary file in its directory,
    so a reader sees the old bytes or the new ones."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.{secrets.token_hex(4)}.tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def require_read(context: ToolContext, path: Path, action: str, **kwargs: Any) -> str | None:
    """``context``'s ledger refusal for ``action`` on ``path``
    (`ReadLedger.require`), or None when FILE_REQUIRE_READ is off."""
    return context.reads.require(path, action, **kwargs) if context.file_require_read else None


def _merge_line_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if not merged or start > merged[-1][1] + 1:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


@dataclass(frozen=True)
class Coverage:
    """What the model has seen of one file: the hash of the content it read
    (None when it read none), the line ranges it saw, and whether it saw the
    whole file."""

    content_hash: str | None
    ranges: list[tuple[int, int]]
    whole: bool


@dataclass
class _ReadWindow:
    """One path's read coverage since the runtime last settled reads: the
    coverage before the first of those reads, and what each read call added."""

    content_hash: str
    base_ranges: list[tuple[int, int]]
    base_whole: bool
    reads: dict[str, tuple[list[tuple[int, int]], bool]] = field(default_factory=dict)


class ReadLedger:
    """Read coverage of one ToolContext. Read-only calls run in parallel
    threads, so every method takes the ledger's lock."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._file: Path | None = None
        self._read_paths: set[Path] = set()
        self._file_hashes: dict[Path, str] = {}
        self._read_ranges: dict[Path, list[tuple[int, int]]] = {}
        self._read_line_totals: dict[Path, int] = {}
        self._fully_read_paths: set[Path] = set()
        # The bytes js last knew a file to hold, with their hash: set by read
        # and by every tool that writes the file. A later edit that finds the
        # file changed on disk diffs against this instead of asking for a re-read.
        self._known_content: dict[Path, tuple[str, bytes]] = {}
        self._read_windows: dict[Path, _ReadWindow] = {}
        # Line reads whose whole text reached the model. Per path, a read key
        # (content hash, first line, last line, numbered) maps to the read
        # call's id and the sha256 of the text it returned.
        self._shown_reads: dict[Path, dict[tuple, tuple[str, str]]] = {}
        # Reads of the batch still running, by call id: (path, read key,
        # sha256 of the text). settle moves the ones the runtime delivered
        # whole into _shown_reads.
        self._offered_reads: dict[str, tuple[Path, tuple, str]] = {}

    def remember(
        self,
        path: Path,
        content_hash: str,
        *,
        start_line: int | None = None,
        end_line: int | None = None,
        total_lines: int | None = None,
        whole_file: bool | None = None,
    ) -> None:
        """Record the content the model actually saw, not merely its path.

        Callers that omit a range retain the historical meaning of a complete
        read. Text readers pass their rendered window explicitly, including when
        an implicit max-lines page made a nominal whole-file read partial.
        """
        if whole_file is None:
            whole_file = start_line is None and end_line is None
        with self._lock:
            previous_hash = self._file_hashes.get(path)
            if previous_hash is not None and previous_hash != content_hash:
                self._read_ranges.pop(path, None)
                self._read_line_totals.pop(path, None)
                self._fully_read_paths.discard(path)
                self._read_windows.pop(path, None)
                self._shown_reads.pop(path, None)
            # The runtime can clip this read's text after the handler returns;
            # narrow_to_delivered then narrows this call's share and keeps
            # what earlier and concurrent reads delivered.
            window = self._read_windows.get(path)
            if window is None or window.content_hash != content_hash:
                window = _ReadWindow(content_hash, list(self._read_ranges.get(path, [])),
                                     path in self._fully_read_paths)
                self._read_windows[path] = window
            call_id = CALL_ID.get() or f"anonymous-{next(_anonymous_calls)}"
            self._read_paths.add(path)
            self._file_hashes[path] = content_hash
            if total_lines is not None:
                self._read_line_totals[path] = max(0, total_lines)
            if whole_file:
                window.reads[call_id] = ([(1, total_lines)] if total_lines else [], True)
                self._fully_read_paths.add(path)
                if total_lines:
                    self._read_ranges[path] = [(1, total_lines)]
            elif start_line is None or end_line is None or end_line < start_line:
                window.reads[call_id] = ([], False)
            else:
                window.reads[call_id] = ([(start_line, end_line)], False)
                ranges = [*self._read_ranges.get(path, []), (start_line, end_line)]
                self._read_ranges[path] = _merge_line_ranges(ranges)
                total = self._read_line_totals.get(path)
                if total == 0 or (total is not None and self._ranges_cover(path, 1, total)):
                    self._fully_read_paths.add(path)
            self._save()

    def _ranges_cover(
        self,
        path: Path,
        start: int,
        end: int,
        seen_ranges: list[tuple[int, int]] | None = None,
    ) -> bool:
        with self._lock:
            if seen_ranges is None and path in self._fully_read_paths:
                return True
            cursor = start
            for seen_start, seen_end in seen_ranges if seen_ranges is not None else self._read_ranges.get(path, []):
                if seen_end < cursor:
                    continue
                if seen_start > cursor:
                    return False
                cursor = max(cursor, seen_end + 1)
                if cursor > end:
                    return True
            return cursor > end

    def remember_content(self, path: Path, content_hash: str, data: bytes) -> None:
        """Keep *data* as the content of *path* that *content_hash* names."""
        with self._lock:
            self._known_content.pop(path, None)
            self._known_content[path] = (content_hash, data)
            total = sum(len(entry[1]) for entry in self._known_content.values())
            while total > _KNOWN_CONTENT_MAX_BYTES and len(self._known_content) > 1:
                oldest = next(iter(self._known_content))
                total -= len(self._known_content.pop(oldest)[1])

    def require(
        self,
        path: Path,
        action: str,
        *,
        line_ranges: list[tuple[int, int]] | None = None,
        content_hash: str | None = None,
        seen_ranges: list[tuple[int, int]] | None = None,
    ) -> str | None:
        """The refusal for ``action`` on ``path`` when the model has not read
        it, read other content than ``content_hash``, or has not seen every
        line of ``line_ranges`` (checked against ``seen_ranges`` when given);
        None when it has."""
        with self._lock:
            if path not in self._read_paths:
                return f'ERROR: You must read the file with the read tool before attempting to {action}.'
            known_hash = self._file_hashes.get(path)
            if content_hash is not None and known_hash != content_hash:
                return (
                    f'ERROR: {path} changed since it was read (hash {known_hash} when read, {content_hash} now); read it again before attempting to {action}.'
                )
            unseen = [span for span in line_ranges or [] if not self._ranges_cover(path, *span, seen_ranges)]
        if unseen:
            rendered = ", ".join(str(start) if start == end else f"{start}-{end}" for start, end in unseen)
            return (
                f"ERROR: You must read the target line{'s' if len(unseen) != 1 or unseen[0][0] != unseen[0][1] else ''} "
                f"({rendered}) before attempting to {action}."
            )
        return None

    def paths(self) -> list[Path]:
        """Every path the model has read."""
        with self._lock:
            return list(self._read_paths)

    def coverage(self, path: Path) -> Coverage:
        """What the model has seen of ``path``."""
        with self._lock:
            return Coverage(self._file_hashes.get(path), list(self._read_ranges.get(path, [])),
                            path in self._fully_read_paths)

    def content_as_read(self, path: Path) -> tuple[str, bytes] | None:
        """The hash and bytes of the content the model read of ``path``, when
        js still holds that content; None otherwise."""
        with self._lock:
            if path not in self._read_paths:
                return None
            known_hash = self._file_hashes.get(path)
            cached = self._known_content.get(path)
        if cached is None or cached[0] != known_hash:
            return None
        return cached

    def rehash(self, path: Path, content_hash: str) -> None:
        """Record that ``path`` now holds the content ``content_hash`` names,
        leaving the lines counted as seen as they are."""
        with self._lock:
            self._file_hashes[path] = content_hash

    def replace(
        self,
        path: Path,
        content_hash: str,
        ranges: list[tuple[int, int]],
        total_lines: int,
        *,
        whole_file: bool,
    ) -> None:
        """Set what the model has seen of ``path``: ``ranges`` of the content
        ``content_hash`` names, or all of it with ``whole_file``."""
        with self._lock:
            # Reads recorded before this replacement no longer describe the
            # coverage, so a later clip of one of them changes nothing, and
            # none of them is the read a later one repeats.
            self._read_windows.pop(path, None)
            self._forget_shown_reads(path)
            self._read_paths.add(path)
            self._file_hashes[path] = content_hash
            self._read_ranges[path] = _merge_line_ranges(ranges)
            self._read_line_totals[path] = max(0, total_lines)
            if whole_file:
                self._fully_read_paths.add(path)
            else:
                self._fully_read_paths.discard(path)
            self._save()

    def clear(self) -> None:
        """Forget every read."""
        with self._lock:
            self._read_paths.clear()
            self._file_hashes.clear()
            self._read_ranges.clear()
            self._read_line_totals.clear()
            self._fully_read_paths.clear()
            self._known_content.clear()
            self._read_windows.clear()
            self._shown_reads.clear()
            self._offered_reads.clear()

    def persist_to(self, file: Path | None) -> None:
        """Keep the coverage in ``file`` from now on, so js restarted on the
        session still knows what the model saw, and add the coverage a
        previous process recorded there. None keeps it in memory only."""
        with self._lock:
            self._file = file
            self._load()

    def _save(self) -> None:
        """Write the coverage to the ledger's file. Caller holds _lock."""
        if self._file is None:
            return
        rows = {
            str(path): {
                "hash": self._file_hashes[path],
                "ranges": self._read_ranges.get(path, []),
                "total": self._read_line_totals.get(path),
                "whole": path in self._fully_read_paths,
            }
            for path in self._read_paths
            if path in self._file_hashes
        }
        try:
            atomic_write(self._file, json.dumps(rows, separators=(",", ":")).encode("utf-8"))
        except OSError:
            pass

    def _load(self) -> None:
        """Coverage a previous process recorded in the ledger's file. A file
        that changed since stays gated: its hash no longer matches. Caller
        holds _lock."""
        try:
            rows = json.loads(self._file.read_text(encoding="utf-8")) if self._file is not None else {}
        except (OSError, ValueError):
            return
        if not isinstance(rows, dict):
            return
        for raw, row in rows.items():
            try:
                path, content_hash = Path(raw), str(row["hash"])
                ranges = [(int(start), int(end)) for start, end in row.get("ranges") or []]
                total = row.get("total")
            except (KeyError, TypeError, ValueError):
                continue
            self._read_paths.add(path)
            self._file_hashes[path] = content_hash
            if ranges:
                self._read_ranges[path] = _merge_line_ranges(ranges)
            if isinstance(total, int):
                self._read_line_totals[path] = total
            if row.get("whole"):
                self._fully_read_paths.add(path)

    def narrow_to_delivered(self, path: Path, raw: str, delivered: str,
                            call_id: str | None = None) -> None:
        """Reconcile coverage after the runtime clipped a read result.

        The read handler recorded coverage for the whole text it returned, but
        the runtime clips or spills that text before the model sees it. Keep only
        the numbered lines fully present in the shared prefix, so a later edit is
        never authorized against lines the model never received. ``call_id``
        names the read call; it defaults to the call running in this thread."""
        limit = 0
        upper = min(len(raw), len(delivered))
        while limit < upper and raw[limit] == delivered[limit]:
            limit += 1
        # A clip can land mid-line; that line's content was only partly shown.
        complete = raw[:limit].rsplit("\n", 1)[0]
        seen: list[tuple[int, int]] = []
        for line in complete.splitlines():
            head, separator, _text = line.partition("|")
            if separator and head.isdigit():
                number = int(head)
                seen.append((number, number))
        call_id = call_id or CALL_ID.get()
        with self._lock:
            if call_id is not None:
                self._offered_reads.pop(call_id, None)
            window = self._read_windows.get(path)
            if window is None or call_id not in window.reads:
                return
            # Restrict only this read's contribution: keep the coverage that
            # predates the window and every other read's share, so a clip
            # cannot revoke lines another read delivered in full.
            window.reads[call_id] = (seen, False)
            merged = _merge_line_ranges(
                [*window.base_ranges, *(span for ranges, _whole in window.reads.values() for span in ranges)]
            )
            if merged:
                self._read_ranges[path] = merged
            else:
                self._read_ranges.pop(path, None)
            if window.base_whole or any(whole for _ranges, whole in window.reads.values()):
                self._fully_read_paths.add(path)
            else:
                self._fully_read_paths.discard(path)
            self._save()

    def settle(self) -> None:
        """Forget which call recorded which read, and record the reads the
        batch delivered whole as shown. The runtime calls it once a batch's
        results are final, after the last clip."""
        with self._lock:
            self._read_windows.clear()
            for call_id, (path, key, digest) in self._offered_reads.items():
                self._shown_reads.setdefault(path, {})[key] = (call_id, digest)
            self._offered_reads.clear()

    def shown_by(self, path: Path, key: tuple) -> str | None:
        """The id of the read call that showed the model the text read key
        ``key`` of ``path`` renders, when that result is still in its
        history; None otherwise. A call whose result goes to code gets None:
        the code needs the lines, not a note about the model's history."""
        if not FOR_MODEL.get():
            return None
        with self._lock:
            entry = self._shown_reads.get(path, {}).get(key)
        return entry[0] if entry else None

    def offer(self, path: Path, key: tuple, text: str) -> None:
        """Record that the read call running in this thread returned ``text``
        for read key ``key`` of ``path``. It counts as shown once the runtime
        settles the batch without clipping it. A read outside a tool call, or
        one whose result goes to code, records nothing."""
        call_id = CALL_ID.get()
        if call_id is None or not FOR_MODEL.get():
            return
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        with self._lock:
            self._offered_reads[call_id] = (path, key, digest)

    def keep_shown(self, results: dict[str, str]) -> None:
        """Keep the shown reads whose text is still in the model's history.

        ``results`` maps tool call ids to the result text the history holds
        for them. A read whose result was cleared, summarised or dropped is
        forgotten, so a later read of the same lines returns them in full."""
        with self._lock:
            for path in list(self._shown_reads):
                kept = {
                    key: (call_id, digest)
                    for key, (call_id, digest) in self._shown_reads[path].items()
                    if call_id in results
                    and hashlib.sha256(results[call_id].encode("utf-8")).hexdigest() == digest
                }
                if kept:
                    self._shown_reads[path] = kept
                else:
                    del self._shown_reads[path]

    def _forget_shown_reads(self, path: Path) -> None:
        self._shown_reads.pop(path, None)
        for call_id in [cid for cid, offer in self._offered_reads.items() if offer[0] == path]:
            del self._offered_reads[call_id]
