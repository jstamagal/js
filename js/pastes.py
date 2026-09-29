"""Large bracketed pastes collapse to one marker in the input line.

A paste of more than `ui.paste_collapse_lines` lines, or more than
`ui.paste_collapse_chars` characters, is kept here and the input line shows
`[paste #N +X lines]` (or `[paste #N X chars]` when only the character limit
is passed) in its place (pi's editor). When the line is sent, `expand`
puts every kept paste back, so the model reads the full text. Pastes are kept
for the life of the process, so a marker recalled from history still expands.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping

from prompt_toolkit.enums import DEFAULT_BUFFER
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys

from . import settings as _settings

_MARKER = re.compile(r"\[paste #(\d+)(?: \+\d+ lines| \d+ chars)\]")
_pastes: dict[int, str] = {}


def normalize(data: str) -> str:
    """Pasted text with its line endings made `\\n`, as prompt_toolkit does."""
    return data.replace("\r\n", "\n").replace("\r", "\n")


def marker(n: int, text: str, line_limit: int) -> str:
    lines = text.count("\n") + 1
    if line_limit > 0 and lines > line_limit:
        return f"[paste #{n} +{lines} lines]"
    return f"[paste #{n} {len(text)} chars]"


def collapses(text: str, line_limit: int, char_limit: int) -> bool:
    """Whether a paste of ``text`` shows as a marker. A limit of 0 is no limit."""
    lines = text.count("\n") + 1
    return (line_limit > 0 and lines > line_limit) or (char_limit > 0 and len(text) > char_limit)


def keep(text: str, line_limit: int) -> str:
    """Keep ``text`` and return the marker that stands for it."""
    n = len(_pastes) + 1
    _pastes[n] = text
    return marker(n, text, line_limit)


def expand(line: str) -> str:
    """``line`` with each kept paste's marker replaced by the paste, in one
    pass: a marker inside a paste stays as it is."""
    if "[paste #" not in line:
        return line

    def fill(match: re.Match) -> str:
        return _pastes.get(int(match.group(1)), match.group(0))

    return _MARKER.sub(fill, line)


def insert(buffer, data: str, settings: Mapping | None) -> None:
    """Insert pasted ``data`` at the cursor: the marker for a large paste into
    the input line (the buffer named DEFAULT_BUFFER), the text itself
    otherwise. The ex line and the history search never expand markers."""
    text = normalize(data)
    line_limit = int(_settings.knob(settings, "ui.paste_collapse_lines") or 0)
    char_limit = int(_settings.knob(settings, "ui.paste_collapse_chars") or 0)
    if buffer.name == DEFAULT_BUFFER and collapses(text, line_limit, char_limit):
        text = keep(text, line_limit)
    buffer.insert_text(text)


def key_bindings(get_settings: Callable[[], Mapping | None]) -> KeyBindings:
    """The bracketed-paste binding; limits are read on every paste."""
    kb = KeyBindings()

    @kb.add(Keys.BracketedPaste)
    def _paste(event) -> None:
        insert(event.current_buffer, event.data, get_settings())

    return kb
