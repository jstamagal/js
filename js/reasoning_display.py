"""Presentation-only reasoning output, separate from answers and session storage."""

from __future__ import annotations

from typing import Protocol

from . import fset
from .context_budget import estimate_text_tokens
from .display import clean


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
    """Streaming fallback for terminals without the editable scrollback view.
    COLOR_REASONING on a terminal; a stream that is not one gets plain text."""

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
        self._chunks: list[str] = []

    def _write(self, text: str) -> None:
        if self.level == 0:
            return
        write = getattr(self.stream, "write_unlogged", self.stream.write)
        try:
            write(grey(text) if self.tty else clean(text))
            self.stream.flush()
        except (OSError, ValueError):
            # A closed or broken stderr (`js -p ... 2>&1 | head`): reasoning is
            # presentation only, so it stops showing and the model call goes on.
            self.level = 0

    def append(self, text: str) -> None:
        if not text or self.level == 0:
            return
        if not self.started or self.closed:
            self._write("── reasoning ──\n")
            self.started = True
            self.closed = False
        self._chunks.append(text)
        self._write(text)
        self.last_newline = text.endswith("\n")

    def answer_started(self) -> None:
        if self.started and not self.closed:
            if not self.last_newline:
                self._write("\n")
            self.closed = True

    def finish(self, tokens: int | None = None) -> None:
        self.answer_started()
        if self.started and self.level == 3:
            count = str(tokens) if tokens is not None else f"~{estimate_text_tokens(''.join(self._chunks))}"
            self._write(f"── reasoning: {count} tok ──\n")
