"""Visible-screen transcript logging.

This is intentionally separate from the raw session JSONL and the debug request
autolog. It records visible answers and tool output with IRC-style speaker tags
for user and assistant turns. Display-only reasoning bypasses this sink; its
original text lives in the session JSONL.
"""

from __future__ import annotations

import contextlib
import sys
import threading
import weakref
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from . import display as _display
from . import fset
from . import messages as msgs
from . import toolkit as _toolkit

def strip_ansi(text: str) -> str:
    """``text`` without terminal escapes, the ones `display.clean` removes,
    so no write puts an escape into the log. Other control bytes stay, so the
    log keeps carriage returns and tabs as they were shown."""
    cleaner = _display.StreamCleaner(controls=True)
    return cleaner.feed(text) + cleaner.flush()


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
        # One escape cleaner per stream, each plain source (`plain:NAME`) and
        # each speaker, so an escape cut between two writes is still taken
        # whole and one stream's open escape never swallows another's text.
        self._cleaners: dict[str, _display.StreamCleaner] = {}

    def _cleaned(self, stream: str, text: str) -> str:
        cleaner = self._cleaners.setdefault(stream, _display.StreamCleaner(controls=True))
        return cleaner.feed(text)

    def _flushed(self, stream: str) -> str:
        cleaner = self._cleaners.pop(stream, None)
        return cleaner.flush() if cleaner is not None else ""

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

    def write_plain(self, text: str, *, source: str = "") -> None:
        """Write non-speaker visible text, line-prefixed with a cheap timestamp.
        ``source`` names the stream it came from: each has its own cleaner."""
        if not text or self.is_muted():
            return
        with self._lock:
            self._plain_locked(self._cleaned(f"plain:{source}", str(text)))

    def _plain_locked(self, clean: str) -> None:
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
        self.write_turn("USER", text)

    def write_assistant(self, text: str) -> None:
        self.write_turn("APE", text)

    def write_assistant_chunk(self, text: str) -> None:
        self._write_speaker_chunk("APE", text)

    def end_assistant_stream(self) -> None:
        self.end_speaker_stream("APE")

    def _write_speaker_chunk(self, speaker: str, text: str) -> None:
        if not text:
            return
        with self._lock:
            self._speaker_locked(speaker, self._cleaned(speaker, str(text)))

    def _speaker_locked(self, speaker: str, clean: str) -> None:
        prefix = f"<{speaker}> "
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
            self._speaker_locked(speaker, self._flushed(speaker))
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
            for stream in list(self._cleaners):
                if stream.startswith("plain:"):
                    self._plain_locked(self._flushed(stream))
                else:
                    self._speaker_locked(stream, self._flushed(stream))
            for speaker in list(self._speaker_at_line_start):
                if not self._speaker_at_line_start.get(speaker, True):
                    self._write_locked("\n")
                    self._speaker_at_line_start[speaker] = True
            self._disable_locked()


class ToolRecordsOnly:
    """A transcript as a run sees it that records its answer and user line
    itself afterwards (plain `js -p`): the tool calls go in, whole, as they
    finish; the streamed answer does not, so it is not recorded twice."""

    def __init__(self, sink: TranscriptLogSink) -> None:
        self._sink = sink

    def write_plain(self, text: str, *, source: str = "") -> None:
        self._sink.write_plain(text, source=source)

    def mute_tee(self):
        return self._sink.mute_tee()

    def flush(self) -> None:
        self._sink.flush()


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
            sink.write_plain(text, source=f"tee:{id(self)}")

    def write_unlogged(self, text: str) -> None:
        """Write display-only material without feeding any nested transcript tee."""
        try:
            write = getattr(self._primary, "write_unlogged", self._primary.write)
            write(text)
        except Exception:
            pass

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


class TranscriptSink:
    """The transcript log's record of a turn's output events: every traced tool
    call whole as it finishes, the streamed answer, and the lines selected by
    transcript stdio capture. It writes to the
    telemetry's ``transcript_log``, the one it had when the answer's turn
    began; a log without a method for a record (`ToolRecordsOnly` has no
    answer) skips that record. What the console draws does not change what is
    recorded."""

    def __init__(self, telemetry: Any) -> None:
        self._telemetry = telemetry
        # Per turn: [the log its answer goes to, an answer is being written].
        self._streams: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()

    def emit(self, event: Any) -> None:
        handler = getattr(self, f"_on_{event.name}", None)
        if handler is not None:
            handler(**dict(event.fields))

    def _plain(self, text: str, *, stderr: bool = False) -> None:
        log = getattr(self._telemetry, "transcript_log", None)
        stream = sys.stderr if stderr else sys.stdout
        seen = set()
        while stream is not None and id(stream) not in seen:
            seen.add(id(stream))
            if isinstance(stream, TranscriptTee) and stream._sink_getter() is log and log is not None:
                log.write_plain(text, source=f"tee:{id(stream)}")
                return
            stream = getattr(stream, "_primary", None)

    def _on_run_line(self, *, fields: list[str]) -> None:
        parts = msgs.RUN_LINE.runs(fields="".join(
            _display.FIELD_BREAK + _display.clean(field) for field in fields))
        for row in _display.field_rows(parts, _display.block_width()):
            self._plain(fset.to_ansi(row) + "\n")

    def _on_call_stats(self, *, label: str, elapsed_s: float, finish: Any, tool_calls: int, tokens: int,
                       first_token_s: float | None, prompt_tokens: int, cached_tokens: int) -> None:
        tps = (tokens / elapsed_s) if elapsed_s > 0 else 0.0
        cache = ""
        if prompt_tokens > 0 and cached_tokens > 0:
            cache = f"  cache {100.0 * cached_tokens / prompt_tokens:.0f}%"
        ttft = f"  ttft {int(first_token_s * 1000)}ms" if first_token_s is not None else ""
        self._plain(msgs.CALL_STATS.line(
            label=f"{label}: " if label else "", ms=int(elapsed_s * 1000),
            finish=finish, tool_calls=tool_calls, tokens=tokens, tps=tps, ttft=ttft, cache=cache) + "\n")

    def _on_notice(self, *, message: msgs.Message, fields: dict, stderr: bool, flush: bool) -> None:
        self._plain(message.line(**fields) + "\n", stderr=stderr)

    def _on_network(self, *, level: int, line: Any, fallback: bool) -> None:
        from . import stream_transport

        if fallback and stream_transport._sink is None:
            self._plain(stream_transport._net_line(line) + "\n", stderr=True)

    def _on_tool_result(self, *, name: str, args: dict | None, result: Any, malformed: bool,
                        context: Any, traced: bool = True, **_fields: Any) -> None:
        if not traced:
            return
        write_plain = getattr(getattr(self._telemetry, "transcript_log", None), "write_plain", None)
        if not callable(write_plain):
            return
        settings = getattr(getattr(context or _toolkit.STOCK_CONTEXT, "config", None), "settings", None)
        level = _display.tools_level(settings)
        if level <= 0:
            return
        # Every call whole, at every level.
        if level < 3:
            record = _display.render_exchange(name, args, result, level=3, malformed=malformed, settings=settings)
        else:
            record = _display.render_exchange(name, args, result, level=level,
                                              preview=_display.preview_lines(settings),
                                              malformed=malformed, settings=settings)
        write_plain(_display.for_transcript(record))

    def _on_stream_open(self, *, stream: Any) -> None:
        self._streams[stream] = [getattr(self._telemetry, "transcript_log", None), False]

    def _on_answer(self, *, stream: Any, text: str, suppressed: bool, **_fields: Any) -> None:
        if suppressed:
            return
        if stream not in self._streams:
            self._on_stream_open(stream=stream)
        record = self._streams[stream]
        record[1] = True
        write_chunk = getattr(record[0], "write_assistant_chunk", None)
        if callable(write_chunk):
            write_chunk(text)

    def _on_answer_end(self, *, stream: Any, suppressed: bool) -> None:
        record = self._streams.get(stream)
        if record is None or suppressed or not record[1]:
            return
        record[1] = False
        end_stream = getattr(record[0], "end_assistant_stream", None)
        if callable(end_stream):
            end_stream()


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
