"""Presentation-only reasoning output, separate from answers and session storage."""

from __future__ import annotations

from typing import Protocol

from . import fset
from . import messages as msgs
from .display import StreamCleaner, clean, terminal_width


class ReasoningDisplay(Protocol):
    def append(self, text: str) -> None: ...
    def answer_started(self) -> None: ...
    def finish(self, tokens: int | None = None) -> None: ...


def grey(text: str) -> str:
    """Reasoning text in COLOR_REASONING, control sequences removed."""
    text = clean(text)
    code = fset.fg("COLOR_REASONING")
    rendered = "\n".join(f"{code}{line}{fset.RESET}" if line else line for line in text.split("\n"))
    return rendered


class StderrReasoning:
    """Reasoning on stderr, for a run without the screen (`js -p`, the
    blocking REPL). Nothing there can unfold a block, so nothing folds: at
    REASONING_DISPLAY 0 nothing shows; at 1, 2 and 3 the heading, the text,
    and at the end `N tok reasoning` when the provider gave a count. On a terminal
    the text is word-wrapped to its width in COLOR_REASONING, each row shown
    once it is full; a stream that is not one gets the plain text as it
    arrives."""

    def __init__(self, level: int, stream) -> None:
        self.level = level
        self.stream = stream
        try:
            self.tty = bool(stream.isatty())
        except (AttributeError, ValueError, OSError):
            self.tty = False
        self.started = False
        self.closed = False
        self.last_newline = True
        self._cleaner = StreamCleaner()
        self._line = ""  # the text of the line not yet ended, held to wrap it whole
        self._shown = 0  # how much of it, tabs expanded, is on the stream

    def _write_heading(self, heading: str) -> None:
        self._emit(grey(heading + "\n") if self.tty else fset.plain(heading) + "\n")

    def _write(self, text: str) -> None:
        """Cleaned reasoning text: as it arrives off a terminal; on one,
        word-wrapped to the window, a row shown once the text after it starts
        the next row, so nothing more can join it."""
        if not self.tty:
            self._emit(text)
            return
        *done, last = (self._line + text).split("\n")
        for line in done:
            self._line = line
            self._show_rows(whole=True)
        self._line = last
        self._show_rows(whole=False)

    def _show_rows(self, *, whole: bool) -> None:
        """The held line's rows not shown yet, wrapped to the window as it is
        now; all of them when the line has ended (``whole``), else all but
        the last, which may grow. What is shown is never wrapped again, so a
        resize changes only the rows still to come."""
        width = terminal_width() - 1
        rest = self._line[self._shown:]
        rows: list[str] = []
        used = 0
        while len(rest) - used > width:
            cut = rest.rfind(" ", used, used + width + 1)
            if cut <= used:
                break
            rows.append(rest[used:cut].rstrip())
            used = cut + 1
        if whole:
            rows.append(rest[used:])
            used = len(rest)
        if rows:
            self._emit(grey("\n".join(rows) + "\n"))
            self._shown += used
        if whole:
            self._line, self._shown = "", 0

    def _end_line(self) -> None:
        if self._line:
            self._show_rows(whole=True)
        elif not self.tty and not self.last_newline:
            self._emit("\n")

    def _emit(self, text: str) -> None:
        if self.level == 0:
            return
        write = getattr(self.stream, "write_unlogged", self.stream.write)
        try:
            write(text)
            self.stream.flush()
        except (OSError, ValueError):
            # A closed or broken stderr (`js -p ... 2>&1 | head`): reasoning is
            # presentation only, so it stops showing and the model call goes on.
            self.level = 0

    def append(self, text: str) -> None:
        if not text or self.level == 0:
            return
        text = self._cleaner.feed(text)
        if not self.started or self.closed:
            self._write_heading(msgs.REASONING_HEADING.body())
            self.started = True
            self.closed = False
        if text:
            self._write(text)
            self.last_newline = text.endswith("\n")

    def answer_started(self) -> None:
        if self.started and not self.closed:
            self._end_line()
            self.closed = True

    def finish(self, tokens: int | None = None) -> None:
        self.answer_started()
        if not self.started:
            return
        if tokens is not None:
            count = msgs.REASONING_COUNT.body(tokens=f"{tokens:,}")
            self._write_heading(f"{count} {msgs.REASONING_HEADING.body()}")
            self.started = False
