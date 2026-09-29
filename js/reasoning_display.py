"""Presentation-only reasoning output, separate from answers and session storage."""

from __future__ import annotations

from typing import Protocol

from . import colors as C
from .context_budget import estimate_text_tokens
from .display import clean


class ReasoningDisplay(Protocol):
    def append(self, text: str) -> None: ...
    def answer_started(self) -> None: ...
    def finish(self, tokens: int | None = None) -> None: ...


def grey(text: str) -> str:
    """Reasoning text in grey, control sequences removed."""
    text = clean(text)
    rendered = "\n".join(f"{C.GREY}{line}{C.RESET}" for line in text.split("\n"))
    return rendered.removesuffix(C.GREY + C.RESET) if text.endswith("\n") else rendered


class StderrReasoning:
    """Streaming fallback for terminals without the editable scrollback view."""

    def __init__(self, level: int, stream) -> None:
        self.level = level
        self.stream = stream
        self.started = False
        self.closed = False
        self.last_newline = True
        self._chunks: list[str] = []

    def _write(self, text: str) -> None:
        write = getattr(self.stream, "write_unlogged", self.stream.write)
        write(grey(text))
        self.stream.flush()

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
