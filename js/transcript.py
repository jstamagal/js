"""Visible-screen transcript logging.

This is intentionally separate from the raw session JSONL and the debug request
autolog. It records what the operator saw, with only IRC-style speaker tags
added for user and assistant turns.
"""

from __future__ import annotations

import contextlib
import io
import re
import threading
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.text import Text


_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def render_plain(obj: object) -> str:
    """Best-effort plain text for a Rich/Textual renderable."""
    if isinstance(obj, Text):
        return obj.plain
    if isinstance(obj, str):
        buf = io.StringIO()
        Console(
            file=buf,
            force_terminal=False,
            color_system=None,
            width=10_000,
            highlight=False,
        ).print(obj, markup=True, highlight=False, end="")
        return strip_ansi(buf.getvalue())
    buf = io.StringIO()
    Console(
        file=buf,
        force_terminal=False,
        color_system=None,
        width=10_000,
        highlight=False,
    ).print(obj, highlight=False, end="")
    return strip_ansi(buf.getvalue())


class TranscriptLogSink:
    """Thread-safe append-only transcript sink.

    Every write swallows IO failures. A failing file disables the whole sink so a
    bad log path never crashes or slows later turns with repeated errors.
    """

    def __init__(self, files: list, paths: list[Path]) -> None:
        self._files = files
        self.paths = paths
        self._lock = threading.Lock()
        self._plain_at_line_start = True
        self._speaker_at_line_start: dict[str, bool] = {}
        self._mute = threading.local()

    def _stamp(self) -> str:
        return datetime.now().strftime("[%H:%M] ")

    def _disable_locked(self) -> None:
        for f in self._files:
            try:
                f.close()
            except Exception:
                pass
        self._files = []

    def _write_locked(self, text: str) -> None:
        if not text or not self._files:
            return
        try:
            for f in self._files:
                f.write(text)
                f.flush()
        except Exception:
            self._disable_locked()

    def write_plain(self, text: str) -> None:
        """Write non-speaker visible text, line-prefixed with a cheap timestamp."""
        if not text or self.is_muted():
            return
        clean = strip_ansi(str(text))
        with self._lock:
            for part in clean.splitlines(keepends=True):
                if self._plain_at_line_start:
                    self._write_locked(self._stamp())
                    self._plain_at_line_start = False
                if part.endswith("\n"):
                    self._write_locked(part)
                    self._plain_at_line_start = True
                else:
                    self._write_locked(part)

    def write_turn(self, speaker: str, text: str) -> None:
        self._write_speaker_chunk(speaker, text)
        self.end_speaker_stream(speaker)

    def write_user(self, text: str) -> None:
        self.write_turn("KING", text)

    def write_assistant(self, text: str) -> None:
        self.write_turn("APE", text)

    def write_assistant_chunk(self, text: str) -> None:
        self._write_speaker_chunk("APE", text)

    def end_assistant_stream(self) -> None:
        self.end_speaker_stream("APE")

    def _write_speaker_chunk(self, speaker: str, text: str) -> None:
        if not text:
            return
        clean = strip_ansi(str(text))
        prefix = f"<{speaker}> "
        with self._lock:
            at_line_start = self._speaker_at_line_start.get(speaker, True)
            for part in clean.splitlines(keepends=True):
                if at_line_start:
                    self._write_locked(self._stamp() + prefix)
                    at_line_start = False
                if part.endswith("\n"):
                    self._write_locked(part)
                    at_line_start = True
                else:
                    self._write_locked(part)
            self._speaker_at_line_start[speaker] = at_line_start

    def end_speaker_stream(self, speaker: str) -> None:
        with self._lock:
            if not self._speaker_at_line_start.get(speaker, True):
                self._write_locked("\n")
                self._speaker_at_line_start[speaker] = True

    def flush(self) -> None:
        with self._lock:
            for f in self._files:
                try:
                    f.flush()
                except Exception:
                    self._disable_locked()
                    break

    def isatty(self) -> bool:
        return False

    def is_muted(self) -> bool:
        return bool(getattr(self._mute, "depth", 0))

    @contextlib.contextmanager
    def mute_tee(self) -> Iterator[None]:
        self._mute.depth = int(getattr(self._mute, "depth", 0)) + 1
        try:
            yield
        finally:
            self._mute.depth -= 1

    def close(self) -> None:
        with self._lock:
            for speaker in list(self._speaker_at_line_start):
                if not self._speaker_at_line_start.get(speaker, True):
                    self._write_locked("\n")
                    self._speaker_at_line_start[speaker] = True
            self._disable_locked()


class TranscriptTee:
    """Write to a real stream and the current transcript sink."""

    def __init__(self, primary, sink_getter: Callable[[], TranscriptLogSink | None]) -> None:
        self._primary = primary
        self._sink_getter = sink_getter

    def write(self, text: str) -> None:
        try:
            self._primary.write(text)
        except Exception:
            pass
        sink = self._sink_getter()
        if sink is not None:
            sink.write_plain(text)

    def flush(self) -> None:
        try:
            self._primary.flush()
        except Exception:
            pass
        sink = self._sink_getter()
        if sink is not None:
            sink.flush()

    def isatty(self) -> bool:
        try:
            return bool(self._primary.isatty())
        except Exception:
            return False


def open_transcript_sink(paths: list[Path]) -> TranscriptLogSink | None:
    files: list = []
    opened: list[Path] = []
    for path in paths:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            files.append(open(path, "a", encoding="utf-8"))  # noqa: SIM115 - sink owns lifetime
            opened.append(path)
        except OSError:
            pass
    if not files:
        return None
    return TranscriptLogSink(files, opened)
