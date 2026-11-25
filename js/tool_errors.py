"""A turn's tool errors: the same error from the same tool, counted across a turn's calls."""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field

from . import (
    jail as _jail,
    settings as _settings,
)
from .toolkit.core import Output

# Quoted text, paths and numbers vary between two failures that are the same
# mistake; `_error_signature` blanks them.
_ERROR_QUOTED = re.compile(r"""'[^'\n]*'|"[^"\n]*"|`[^`\n]*`""")


_ERROR_PATH = re.compile(r"\S*/\S*")


_ERROR_NUMBER = re.compile(r"\d+")


def _error_signature(result: str) -> str:
    """``result`` with quoted text, paths and numbers blanked and whitespace
    collapsed: two errors with one signature are the same error."""
    text = _ERROR_QUOTED.sub("''", result)
    text = _ERROR_PATH.sub("/", text)
    text = _ERROR_NUMBER.sub("0", text)
    return " ".join(text.split())


@dataclass
class _ErrorStreak:
    signature: str
    count: int = 1
    # The count when the model was told to change approach; None until then.
    steered_at: int | None = None


@dataclass
class ToolErrorTracker:
    """Counts, per tool, the ERROR results in a row that carry the same
    error. At `limit` the model is told once to change approach; one more
    repeat of that error ends the turn. A different error or a result that
    is not an error starts the count over."""
    limit: int = field(default_factory=lambda: _settings.default_value("TOOL_ERRORS_MAX"))
    errors: dict[str, _ErrorStreak] = field(default_factory=dict)
    # Under the REPL's supervisor a turn's leaf tools dispatch in an
    # executor thread while its fan-out calls run as coroutines on the loop; both
    # record into this tracker, so guard the counter against that cross-thread
    # interleave.
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    @classmethod
    def from_settings(cls, settings: dict | None) -> ToolErrorTracker:
        return cls(limit=int(_settings.knob(settings, "TOOL_ERRORS_MAX")))

    def record(self, tool_name: str, result: str) -> str:
        if isinstance(result, _jail.Refusal):
            return result
        with self._lock:
            if isinstance(result, Output) or not result.startswith("ERROR"):
                self.errors.pop(tool_name, None)
                return result
            signature = _error_signature(result)
            streak = self.errors.get(tool_name)
            if streak is not None and streak.signature == signature:
                streak.count += 1
            else:
                streak = self.errors[tool_name] = _ErrorStreak(signature)
            attempts_left = max(self.limit - streak.count, 0)
            return f'{result}\n<retry>attempts_left={attempts_left}, allowed_max_attempts={self.limit}</retry>'

    def steer_due(self) -> list[tuple[str, int]]:
        """(tool, repeats) for each tool whose same error reached the limit
        and that has not been told to change approach yet; marks them told."""
        due: list[tuple[str, int]] = []
        with self._lock:
            for name, streak in self.errors.items():
                if streak.steered_at is None and streak.count >= self.limit:
                    streak.steered_at = streak.count
                    due.append((name, streak.count))
        return due

    def exhausted(self) -> str | None:
        """The tool that repeated its error after being told to change
        approach, or None."""
        with self._lock:
            return next((name for name, streak in self.errors.items()
                         if streak.steered_at is not None and streak.count > streak.steered_at), None)

    def limit_reached(self) -> bool:
        return self.exhausted() is not None
