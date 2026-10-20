"""Terminal presentation: model text and tool exchanges on their way to a screen.

Every byte of model or tool output passes `clean()` before a terminal sees it,
so a model that cats a binary cannot leave the terminal needing `reset`.

`Display` owns one stream of answer text. On a terminal it renders Markdown:
finished top-level blocks commit once and are never redrawn; only the last,
still-open block is live. Anywhere else it writes the cleaned text as it
arrives.

The tool renderers build the exchange a turn prints at TOOLS_DISPLAY 0-3: a
header (mark, tool name, key argument, the other arguments dim), the program
or output in a panel, a file or a diff as a numbered view, and a dim footer
with the metrics. The colours are /fset's `COLOR_*` entries (`js.fset`).
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import shutil
import time
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Protocol

from prompt_toolkit.utils import get_cwidth
from rich.console import Console
from rich.markdown import Markdown, TableElement
from rich.syntax import Syntax
from rich.text import Text
from rich.theme import Theme as _RichTheme

from . import fset
from . import messages as msgs
from . import output
from . import settings as _settings

_SGR_ONLY = re.compile(r"\x1b\[[0-9;]*m")

# The states of the DEC/ECMA-48 escape parser (Paul Williams, "A parser for
# DEC's ANSI-compatible video terminals"). Only ground prints.
GROUND, ESCAPE, ESCAPE_INTERMEDIATE = "ground", "escape", "escape_intermediate"
CSI_ENTRY, CSI_PARAM, CSI_INTERMEDIATE, CSI_IGNORE = "csi_entry", "csi_param", "csi_intermediate", "csi_ignore"
DCS_ENTRY, DCS_PARAM, DCS_INTERMEDIATE = "dcs_entry", "dcs_param", "dcs_intermediate"
DCS_PASSTHROUGH, DCS_IGNORE = "dcs_passthrough", "dcs_ignore"
OSC_STRING, SOS_PM_APC_STRING = "osc_string", "sos_pm_apc_string"
_STRINGS = frozenset({DCS_ENTRY, DCS_PARAM, DCS_INTERMEDIATE, DCS_PASSTHROUGH, DCS_IGNORE, OSC_STRING,
                      SOS_PM_APC_STRING})

# Ground: the next byte that is not printed as it is (C0 but newline and tab,
# DEL, C1). A string: the next byte that can end it (BEL, CAN, SUB, ESC, C1).
_GROUND_STOP = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_STRING_STOP = re.compile(r"[\x07\x18\x1a\x1b\x80-\x9f]")
# Ground, for a cleaner that keeps the other controls: ESC and C1 only.
_ESCAPE_STOP = re.compile(r"[\x1b\x80-\x9f]")
_UNPRINTED = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

# Where ESC and each C1 introducer lead, from any state.
_AFTER_ESC = {"[": CSI_ENTRY, "]": OSC_STRING, "P": DCS_ENTRY, "X": SOS_PM_APC_STRING, "^": SOS_PM_APC_STRING,
              "_": SOS_PM_APC_STRING}
_C1 = {"\x9b": CSI_ENTRY, "\x9d": OSC_STRING, "\x90": DCS_ENTRY, "\x98": SOS_PM_APC_STRING,
       "\x9e": SOS_PM_APC_STRING, "\x9f": SOS_PM_APC_STRING}


def _sequence_step(state: str, char: str) -> str:
    """The state after ``char`` in one of the non-string sequence states.
    A character past 7-bit ASCII ends the sequence."""
    code = ord(char)
    if code >= 0xA0:
        return GROUND
    if code < 0x20 or code == 0x7F:
        return state  # executed or ignored: the sequence goes on
    if state == ESCAPE:
        if char in _AFTER_ESC:
            return _AFTER_ESC[char]
        return ESCAPE_INTERMEDIATE if code <= 0x2F else GROUND
    if state == ESCAPE_INTERMEDIATE:
        return ESCAPE_INTERMEDIATE if code <= 0x2F else GROUND
    if code >= 0x40:
        return GROUND  # the final byte of a CSI
    if state == CSI_IGNORE:
        return CSI_IGNORE
    if code <= 0x2F:
        return CSI_INTERMEDIATE
    if state == CSI_INTERMEDIATE or char == ":" or (state == CSI_PARAM and code >= 0x3C):
        return CSI_IGNORE
    return CSI_PARAM


def _dcs_step(state: str, char: str) -> str:
    """The state after ``char`` inside a DCS, before its passthrough."""
    code = ord(char)
    if code < 0x20 or code == 0x7F or state in (DCS_PASSTHROUGH, DCS_IGNORE):
        return state
    if code >= 0x40:
        return DCS_PASSTHROUGH
    if code <= 0x2F:
        return DCS_INTERMEDIATE
    if state == DCS_INTERMEDIATE or char == ":" or (state == DCS_PARAM and code >= 0x3C):
        return DCS_IGNORE
    return DCS_PARAM


class StreamCleaner:
    """Model and tool text as text, never as terminal control sequences: the
    DEC/ECMA-48 escape parser, kept across chunks. Ground text is passed on
    (newline and tab with it; every other C0 and C1 control dropped); CSI,
    other escapes and the strings (OSC, DCS, SOS, PM, APC, ended by ST or
    BEL) are dropped whole wherever a chunk cuts them. A string still open
    when the text ends is not a control sequence: its text comes back from
    `flush`. Model and tool text is cleaned here, at intake, and `clean` is
    this run over one whole text, so any chunking cleans the same. With
    ``controls``, ground keeps every C0 control but ESC, and DEL: the
    transcript keeps carriage returns as they were shown."""

    def __init__(self, *, controls: bool = False) -> None:
        self.state = GROUND
        self._string: list[str] = []  # the text of a string still open
        self._controls = controls
        self._ground_stop = _ESCAPE_STOP if controls else _GROUND_STOP

    def feed(self, text: str) -> str:
        out: list[str] = []
        pos, size = 0, len(text)
        while pos < size:
            state = self.state
            if state == GROUND:
                stop = self._ground_stop.search(text, pos)
                end = stop.start() if stop else size
                out.append(text[pos:end])
                pos = end
                if stop is None:
                    break
                char = text[pos]
                pos += 1
                if char == "\x1b":
                    self.state = ESCAPE
                elif char in _C1:
                    self._open(_C1[char])
                continue
            if state in _STRINGS:
                stop = _STRING_STOP.search(text, pos)
                end = stop.start() if stop else size
                piece = text[pos:end]
                if state in (DCS_ENTRY, DCS_PARAM, DCS_INTERMEDIATE):
                    for index, char in enumerate(piece):
                        self._string.append(char)
                        state = _dcs_step(state, char)
                        if state == DCS_PASSTHROUGH or state == DCS_IGNORE:
                            self._string.append(piece[index + 1:])
                            break
                    self.state = state
                else:
                    self._string.append(piece)
                pos = end
                if stop is None:
                    break
                char = text[pos]
                pos += 1
                # BEL, ST, CAN or SUB end the string; ESC or another C1 ends it
                # and starts what it starts.
                self._string = []
                if char == "\x1b":
                    self.state = ESCAPE
                elif char in _C1:
                    self._open(_C1[char])
                else:
                    self.state = GROUND
                continue
            char = text[pos]
            pos += 1
            if char == "\x1b":
                self.state = ESCAPE
            elif char in _C1:
                self._open(_C1[char])
            elif char in "\x18\x1a" or 0x80 <= ord(char) <= 0x9F:
                self.state = GROUND
            else:
                following = _sequence_step(state, char)
                if following in _STRINGS:
                    self._open(following)
                elif following == GROUND and ord(char) >= 0xA0:
                    # Not part of a sequence: the sequence ends and it prints.
                    self.state = GROUND
                    out.append(char)
                else:
                    self.state = following
        return "".join(out)

    def _open(self, state: str) -> None:
        self.state, self._string = state, []

    def flush(self) -> str:
        """The text has ended: an escape sequence still open is dropped, and
        the text of a string never ended is passed on."""
        held = "".join(self._string) if self.state in _STRINGS else ""
        self.state, self._string = GROUND, []
        return held if self._controls else _UNPRINTED.sub("", held)


def clean(text: str) -> str:
    """Model and tool output as text, never as terminal control sequences
    (`StreamCleaner` over the whole text)."""
    cleaner = StreamCleaner()
    return cleaner.feed(text) + cleaner.flush()


# Transcript marker for a tool exchange: typeable ASCII, one per exchange, so a
# grep of a saved transcript finds every exchange the screen showed.
TOOL_MARKER = ">"

# What `field_rows` breaks a line at: put before each field in
# place of its separator before the line is rendered.
FIELD_BREAK = "\x00"


def _slice_runs(parts: list[fset.Run], start: int, end: int) -> list[fset.Run]:
    """The runs covering characters ``start``..``end`` of their plain text."""
    out: list[fset.Run] = []
    position = 0
    for colour, text in parts:
        low, high = max(start - position, 0), min(end - position, len(text))
        if low < high:
            out.append((colour, text[low:high]))
        position += len(text)
    return out


def field_rows(parts: list[fset.Run], width: int, sep: str = "  ") -> list[list[fset.Run]]:
    """Runs whose fields each follow a `FIELD_BREAK` as rows of runs at most
    ``width`` columns wide, broken between two fields; the rows after the
    first start under the first field. A field wider than its row breaks at
    the column and goes on under the first field. A line with no field is
    word-wrapped. Wrapping works on the plain text; the colours ride along."""
    parts = [(colour, text.expandtabs(TAB)) for colour, text in parts]
    plain = fset.to_plain(parts)
    pieces = plain.split(FIELD_BREAK)
    spans: list[tuple[int, int]] = []
    position = 0
    for piece in pieces:
        spans.append((position, position + len(piece)))
        position += len(piece) + len(FIELD_BREAK)
    if len(pieces) == 1:
        rows, start = [], 0
        for index, end in enumerate(cut_points(plain, width)):
            if index:
                # The spaces at a break are the break: they start no row.
                start = end - len(plain[start:end].lstrip(" "))
                if start >= end:
                    continue
            rows.append(_slice_runs(parts, start, len(plain[:end].rstrip(" ")) if end < len(plain) else end))
            start = end
        return rows
    head_low, head_high = spans[0]
    head = plain[head_low:head_high]
    rows: list[list[fset.Run]] = []
    start = 0
    head_cuts = cut_points(head, width) if head else [0]
    for end in head_cuts:
        # The text before the first field wraps like any line.
        rows.append(_slice_runs(parts, head_low + start, head_low + end))
        start = end
    used = cells(head[head_cuts[-2]:] if len(head_cuts) > 1 else head)
    lead = used if used < width // 2 else 0
    for index, (low, high) in enumerate(spans[1:]):
        field = plain[low:high]
        size = cells(field)
        gap = len(sep) if index else 0
        if used + gap + size > width and used > lead:
            rows.append([("", " " * lead)] if lead else [])
            used, gap = lead, 0
        room = width - used - gap
        if gap:
            rows[-1].append(("", " " * gap))
        if size <= room:
            rows[-1] += _slice_runs(parts, low, high)
            used += gap + size
            continue
        # Wider than what is left of the row: it breaks at the column.
        start = 0
        cuts = cut_points(field, max(room, 1), max(width - lead, 1), words=False)
        for number, end in enumerate(cuts):
            if number:
                rows.append([("", " " * lead)] if lead else [])
            rows[-1] += _slice_runs(parts, low + start, low + end)
            start = end
        used = (lead if len(cuts) > 1 else used + gap) + cells(field[cuts[-2] if len(cuts) > 1 else 0:])
    return rows


def drawn_rows(parts: list[fset.Run], width: int, base: str = "") -> list[str]:
    """Runs word-wrapped to ``width`` columns (`field_rows`), each row as
    drawn text; a run with no colour of its own in colour entry ``base``."""
    colour = fset.hex_for(base) if base else None
    parts = [(own or colour or "", text) for own, text in parts]
    return [fset.to_ansi(row) for row in field_rows(parts, width)]


# The two voices of a conversation hang off their marks: column 0 holds only
# the speaker's mark (MARK_USER, MARK_AGENT), then MARK_GAP spaces, and every
# row of the text starts at that one column.
def _speaker(mark_entry: str, colour: str) -> tuple[str, int]:
    mark = fset.lookup(mark_entry) or ""
    spaces = fset.mark_gap()
    return f"{fset.strong(colour, mark)}{' ' * spaces}", cells(mark) + spaces


def user_speaker() -> tuple[str, int]:
    """The operator's mark with its gap, painted, and the text column."""
    return _speaker("MARK_USER", "COLOR_USER_MARK")


def agent_speaker() -> tuple[str, int]:
    """The agent's mark with its gap, painted, and the text column."""
    return _speaker("MARK_AGENT", "COLOR_AGENT_MARK")


def prompt() -> str:
    """The input prompt: the operator's mark."""
    return user_speaker()[0]


def hang(text: str, mark: str, indent: int, *, first: bool = True) -> str:
    """``text`` with ``mark`` before its first row that shows a character
    (when ``first``) and ``indent`` spaces before every other row that draws
    anything, painted spaces included; empty rows stay empty."""
    rows = text.split("\n")
    for index, row in enumerate(rows):
        plain = _SGR_ONLY.sub("", row)
        if not plain:
            continue
        if first and plain.strip():
            rows[index], first = mark + row, False
        else:
            rows[index] = " " * indent + row
    return "\n".join(rows)


# Columns between tab stops.
TAB = 8


def cells(text: str) -> int:
    """Terminal columns ``text`` takes on one line from column 0: colour
    escapes not counted, a tab out to the next stop, a wide character two.
    Every measure in js goes through here."""
    return sum(get_cwidth(char) for char in fset.plain(text).expandtabs(TAB))


def cut_points(plain: str, room: int, rest: int | None = None, *, words: bool = True) -> list[int]:
    """Where ``plain`` (one line, no newline) breaks into rows: the end of
    each row, the last one ``len(plain)``. The first row holds ``room``
    columns, the rest ``rest`` (``room`` by default). With ``words`` a row
    ends after its last space that fits, else at the column; a row always
    holds at least one character. One pass, with running column counts.
    Every wrapping in js goes through here."""
    rest = room if rest is None else rest
    cuts: list[int] = []
    start, used, space, width = 0, 0, -1, room
    widths = [get_cwidth(char) for char in plain]
    for index, cell in enumerate(widths):
        if used + cell > width and index > start:
            end = space + 1 if words and space >= start else index
            cuts.append(end)
            start, space, width, used = end, -1, rest, 0
            # The text carried onto the new row holds no space; cut it again
            # wherever it is wider than that row.
            for carried in range(end, index):
                if used + widths[carried] > width and carried > start:
                    cuts.append(carried)
                    start, used = carried, 0
                used += widths[carried]
            if used + cell > width and index > start:
                cuts.append(index)
                start, used = index, 0
        if plain[index] == " ":
            space = index
        used += cell
    cuts.append(len(plain))
    return cuts


def wrap_words(text: str, width: int) -> str:
    """``text`` with each line wider than ``width`` columns broken at the
    last space that fits, or at the column when no space does. The spaces at
    a break are the break: they end no row and start none."""
    out: list[str] = []
    for line in text.expandtabs(TAB).split("\n"):
        start = 0
        cuts = cut_points(line, width)
        for index, end in enumerate(cuts):
            row = line[start:end]
            start = end
            if index:
                row = row.lstrip(" ")
                if not row:
                    continue
            out.append(row.rstrip(" ") if index < len(cuts) - 1 else row)
    return "\n".join(out)


def user_line(text: str) -> str:
    """One user message as the conversation shows it, word-wrapped to the
    text column."""
    mark, indent = user_speaker()
    text = wrap_words(clean(text), block_width() - indent)
    body = "\n".join(fset.strong("COLOR_USER", line) if line else "" for line in text.split("\n"))
    return hang(body, mark, indent) + "\n"


def echo_user(text: str) -> None:
    """Emit the user text to be echoed where the answer will follow."""
    output.emit(output.event("user_echo", text=text))


def _setting(settings: Any, key: str) -> Any:
    """Knob ``key`` from live settings; its js/jsrc value when absent."""
    return _settings.knob(settings if isinstance(settings, dict) else None, key)


def tools_level(settings: Any) -> int:
    """TOOLS_DISPLAY from live settings: 0 nothing, 1 what matters, 2 preview, 3 whole."""
    level = _setting(settings, "TOOLS_DISPLAY")
    return level if isinstance(level, int) and level in range(4) else _settings.default_value("TOOLS_DISPLAY")


def preview_lines(settings: Any) -> int:
    lines = _setting(settings, "TOOLS_PREVIEW_LINES")
    return lines if isinstance(lines, int) and lines > 0 else _settings.default_value("TOOLS_PREVIEW_LINES")


def markdown_enabled(settings: Any) -> bool:
    return _setting(settings, "MARKDOWN") is not False


def terminal_width() -> int:
    return max(20, shutil.get_terminal_size((80, 24)).columns)


def block_width() -> int:
    """Columns a drawn block may fill: one short of the terminal. The async
    screen's scrollback keeps a cursor cell after every line, so a row as wide
    as the window wraps onto a blank second row."""
    return terminal_width() - 1


def _render_console(width: int, styles: dict[str, str] | None = None, style: str | None = None) -> Console:
    """A console that renders into a string at ``width``: 24-bit colour, none
    under NO_COLOR, ``style`` under everything it draws."""
    return Console(
        theme=_RichTheme(styles) if styles else None,
        style=style,
        file=io.StringIO(),
        force_terminal=True,
        color_system="truecolor",
        no_color=not fset.colour_on(),
        width=width,
        highlight=False,
        markup=False,
        emoji=False,
        soft_wrap=False,
    )


def _rendered_text(console: Console, width: int | None = None) -> str:
    """What ``console`` drew, trailing spaces off each line. With ``width``, a
    line wider than that in `cells` (rich measures some emoji narrower than
    the screen does) is cut again to fit, its colours kept."""
    out = console.file.getvalue()  # type: ignore[attr-defined]
    lines: list[str] = []
    for line in out.split("\n"):
        if width is not None and cells(line) > width:
            text = Text.from_ansi(line)
            text.rstrip()
            if text.cell_len and cells(text.plain) > width:
                lines += [_ansi_of(piece) for piece in text.divide(cut_points(text.plain, width)[:-1])]
                continue
            line = _ansi_of(text)
        lines.append(line.rstrip(" "))
    return "\n".join(lines)


def _ansi_of(text: Text) -> str:
    """``text`` as ANSI, one line, nothing added."""
    console = _render_console(max(cells(text.plain), 1) + 8)
    console.print(text, end="", soft_wrap=True)
    return console.file.getvalue()  # type: ignore[attr-defined]


def _markdown_styles(colours: Theme) -> dict[str, str]:
    """Rich's Markdown styles that carry a colour, in the theme's colours."""
    return {
        "markdown.code": f"{colours.code} on {colours.panel}",
        "markdown.code_block": colours.panel_text,
        "markdown.block_quote": colours.dim,
        "markdown.list": colours.highlight,
        "markdown.item.number": colours.highlight,
        "markdown.h2": f"{fset.setting_value('MARKDOWN_HEADING_TWO_STYLE')} {colours.highlight}",
        "markdown.h3": f"{fset.setting_value('MARKDOWN_HEADING_THREE_STYLE')} {colours.highlight}",
        "markdown.h4": f"{fset.setting_value('MARKDOWN_HEADING_FOUR_STYLE')} {colours.highlight}",
        "markdown.link": colours.highlight,
        "markdown.link_url": f"underline {colours.dim}",
        "markdown.table.border": colours.separator,
        "markdown.table.header": f"not bold {colours.highlight}",
        "markdown.kbd": f"bold {colours.highlight}",
    }


class _FoldingTable(TableElement):
    """A Markdown table whose cells wrap: rich's own cuts a long cell short
    with an ellipsis, and an answer is never cut. A table too wide for the
    width even with its cells wrapped, where rich would drop columns, is
    drawn a row at a time instead: each cell on its own line after its
    column's heading, a rule between rows; a table with no data row, its
    headings a line each."""

    def __rich_console__(self, console: Console, options: Any) -> Any:
        rows = [[cell.content for cell in row.cells] for row in (self.body.rows if self.body else [])]
        headings: list[Text] = []
        if self.header is not None and self.header.row is not None:
            for column in self.header.row.cells:
                heading = column.content.copy()
                heading.stylize("markdown.table.header")
                headings.append(heading)
        if _shows_every_cell(console, options, headings, rows):
            yield _table(headings, rows, len(rows[0]) if rows else 0)
            return
        if not rows:
            # Headings and no data row: the headings, a line each.
            yield from headings
            return
        for index, row in enumerate(rows):
            if index:
                yield Text(fset.setting_value("TABLE_RULE") * min(options.max_width, 3), "markdown.table.border")
            for column, cell in enumerate(row):
                heading = headings[column] if column < len(headings) else Text("")
                yield Text.assemble(heading, " ", cell, overflow="fold") if heading.plain else cell


def _table(headings: list[Text], rows: list[list[Text]], columns: int) -> Any:
    """A Markdown table as rich lays it out, its cells folding."""
    from rich import box
    from rich.table import Table

    table = Table(box=box.SIMPLE, pad_edge=False, style="markdown.table.border", show_edge=True,
                  collapse_padding=True, show_header=bool(headings))
    for heading in headings or [Text("")] * columns:
        table.add_column(heading, overflow="fold")
    for row in rows:
        table.add_row(*row)
    return table


# Private-use characters, one per cell of a table being checked.
_PROBE_BASE = 0xF0000


def _shows_every_cell(console: Console, options: Any, headings: list[Text], rows: list[list[Text]]) -> bool:
    """Whether the table of ``headings`` and ``rows`` drawn at ``options``'
    width shows every cell whole: rich leaves out a column it has no room for.
    The table is drawn once more with each cell's text spelled in a character
    of its own, every character the width it stands for and the spaces kept,
    so the layout is the same; every cell must come back whole."""
    cells_ = [*headings, *(cell for row in rows for cell in row)]
    if len(cells_) > 0xFFFD:
        return False

    def probe(index: int, cell: Text) -> Text:
        mark = chr(_PROBE_BASE + index)
        return Text("".join(char if char.isspace() else mark * max(get_cwidth(char), 1) for char in cell.plain))

    probed = iter(probe(index, cell) for index, cell in enumerate(cells_))
    probe_headings = [next(probed) for _heading in headings]
    probe_rows = [[next(probed) for _cell in row] for row in rows]
    columns = len(rows[0]) if rows else len(headings)
    table = _table(probe_headings, probe_rows, columns)
    drawn = Counter(char for line in console.render_lines(table, options, pad=False)
                    for segment in line for char in segment.text if not char.isspace())
    return all(drawn[chr(_PROBE_BASE + index)]
               == sum(max(get_cwidth(char), 1) for char in cell.plain if not char.isspace())
               for index, cell in enumerate(cells_))


class _Markdown(Markdown):
    """rich's Markdown with tables that wrap their cells."""

    elements = {**Markdown.elements, "table_open": _FoldingTable}


def render_markdown(source: str, width: int, settings: Any = None) -> str:
    """ANSI rendering of Markdown source at `width` columns: no blank lines
    before or after, ending in one newline. Inline code is a chip: its text in
    COLOR_CODE on COLOR_PANEL, a space of padding each side."""
    console = _render_console(width, _markdown_styles(theme(settings)), fset.hex_for("COLOR_AGENT"))
    document = _Markdown(source, code_theme=fset.code_style(), hyperlinks=False)
    for token in document.parsed:
        for child in token.children or ():
            if child.type == "code_inline":
                padding = _setting(settings, "MARKDOWN_INLINE_PADDING")
                child.content = f"{padding}{child.content}{padding}"
            elif child.type == "softbreak" and _setting(settings, "MARKDOWN_HARD_BREAKS"):
                child.type = "hardbreak"
    console.print(document)
    return _rendered_text(console, width).strip("\n") + "\n"


def _block_starts(source: str) -> list[int]:
    """Source line numbers where each top-level Markdown block begins."""
    return [token.map[0] for token in Markdown(source).parsed if token.level == 0 and token.map]


# --------------------------------------------------------------------------
# Answer text
# --------------------------------------------------------------------------


class LiveSurface(Protocol):
    """Where a pretty Display draws. `update` replaces the live region; `commit`
    replaces it with final text and closes it, so the next `update` starts a new
    region below."""

    def width(self) -> int: ...
    def update(self, rendered: str) -> None: ...
    def commit(self, rendered: str) -> None: ...


class TtyLive:
    """Live region on a plain terminal: the tail of the open block, redrawn in
    place with cursor-up and erase-below. Committed text is written once.

    Redraws and erases go through `write`; committed text goes through
    `commit_write` (default `write`), so a stream that logs what it writes can
    keep the final answer and skip the redraws."""

    def __init__(
        self,
        write: Callable[[str], None],
        flush: Callable[[], None] | None = None,
        *,
        commit_write: Callable[[str], None] | None = None,
    ) -> None:
        self._write = write
        self._commit_write = commit_write or write
        self._flush = flush or (lambda: None)
        self._rows = 0

    def width(self) -> int:
        # One column short: a line that fills the last column leaves the cursor
        # in the pending-wrap state and the row count would be off by one.
        return terminal_width() - 1

    def _erase(self) -> None:
        if self._rows:
            self._write(f"\033[{self._rows}F\033[J")
            self._rows = 0

    def update(self, rendered: str) -> None:
        self._erase()
        height = shutil.get_terminal_size((80, 24)).lines
        tail = rendered.rstrip("\n").split("\n")[-max(1, height - 2):]
        self._write("\n".join(tail) + "\n")
        self._rows = len(tail)
        self._flush()

    def commit(self, rendered: str) -> None:
        self._erase()
        self._commit_write(rendered)
        self._rows = 0
        self._flush()


class Display:
    """One stream of answer text.

    `chunk(field, text)` feeds it and `finish()` ends it. With a live surface it
    renders Markdown: each finished top-level block is rendered and committed
    once; the last block stays live until the next block starts or `finish()`.
    Redraws are at most one per `refresh_s`; text that arrives between two is
    drawn when the interval ends, whether or not more text follows.
    Without one it writes the text as it arrives, no cursor movement. Either
    way the text comes in through one `StreamCleaner`.
    """

    def __init__(
        self,
        write: Callable[[str], None],
        *,
        live: LiveSurface | None = None,
        flush: Callable[[], None] | None = None,
        refresh_s: float = 0.1,
        colour: bool = True,
    ) -> None:
        self._write = write
        self._colour = colour  # the stream takes colour escapes
        self._flush = flush or (lambda: None)
        self.live = live
        self.refresh_s = refresh_s
        self.field: str | None = None
        self._source = ""
        self._last_char = "\n"
        self._wrote = False
        self._refreshed = 0.0
        self._trailing: asyncio.TimerHandle | None = None
        self._committed_any = False
        self._speaker: tuple[str, int] | None = None
        self._spoke = False
        self._cleaner = StreamCleaner()

    @classmethod
    def for_stream(cls, stream: Any, *, markdown: bool = True) -> Display:
        redraw = getattr(stream, "write_unlogged", None) or stream.write
        flush = getattr(stream, "flush", None)
        try:
            tty = bool(stream.isatty())
        except Exception:  # noqa: BLE001 - a closed or foreign stream is not a terminal
            tty = False
        live = TtyLive(redraw, flush, commit_write=stream.write) if (markdown and tty) else None
        return cls(stream.write, live=live, flush=flush, colour=tty and fset.colour_on())

    @property
    def pretty(self) -> bool:
        return self.live is not None

    def speaker(self, mark: str, indent: int) -> None:
        """Hang the stream off `mark`: its first row starts with it and every
        other row starts `indent` columns in."""
        self._speaker, self._spoke = (mark if self._colour else fset.plain(mark), indent), False
        self._word, self._spaces, self._row_used, self._row_start = "", 0, 0, True
        self._word_cells = 0

    def _width(self) -> int:
        assert self.live is not None
        return self.live.width() - (self._speaker[1] if self._speaker else 0)

    def _hung(self, rendered: str, *, keep: bool) -> str:
        """``rendered`` hung off the speaker's mark; ``keep`` records that the
        mark has been drawn for good."""
        if self._speaker is None:
            return rendered
        hung = hang(rendered, *self._speaker, first=not self._spoke)
        if keep and _SGR_ONLY.sub("", rendered).strip():
            self._spoke = True
        return hung

    def _hang_plain(self, text: str) -> str:
        """``text`` flowed under the speaker's mark, word-wrapped to the text
        column: a word is held until the space or newline after it shows
        whether it fits on the row, and a word as wide as a whole row goes
        out a row at a time as it grows. A tab is spaces to the next stop."""
        if self._speaker is None:
            return text
        mark, indent = self._speaker
        room = block_width() - indent
        out: list[str] = []
        for char in text:
            if char == "\n":
                out.append(self._flush_word())
                out.append(self._held_spaces() + "\n")
                self._row_used, self._spaces, self._row_start = 0, 0, True
            elif char in " \t":
                out.append(self._flush_word())
                self._spaces += 1 if char == " " else TAB - (self._row_used + self._spaces) % TAB
            else:
                cell = get_cwidth(char)
                if self._word_cells + cell > room:
                    # The held word fills a row by itself: it goes out now.
                    out.append(self._flush_word())
                    out.append("\n" + " " * indent)
                    self._row_used, self._spaces = 0, 0
                self._word += char
                self._word_cells += cell
        return "".join(out)

    def _held_spaces(self) -> str:
        """The spaces held after the row's last word, as many as the row has
        room for, and none held any longer."""
        if self._speaker is None:
            return ""
        room = block_width() - self._speaker[1]
        spaces = " " * min(self._spaces, max(room - self._row_used, 0)) if not self._row_start else ""
        self._spaces = 0
        return spaces

    def _paint(self, text: str) -> str:
        """Answer text in COLOR_AGENT, on a stream that takes colour."""
        return fset.paint("COLOR_AGENT", text) if self._colour else text

    def _flush_word(self) -> str:
        """The held word placed on the current row, or on a new one under
        the text column when it does not fit."""
        if not self._word:
            return ""
        mark, indent = self._speaker  # type: ignore[misc]
        room = block_width() - indent
        out = ""
        if self._row_start:
            out = " " * indent if self._spoke else mark
            self._spoke, self._row_start = True, False
        word = self._word
        size = cells(word)
        if self._row_used + self._spaces + size > room and (not out or self._spaces):
            # It does not fit after what the row holds, its leading spaces
            # included: those stay on this row, the word starts the next.
            out += " " * min(self._spaces, max(room - self._row_used, 0)) + "\n" + " " * indent
            self._row_used, self._spaces = 0, 0
        if self._row_used + self._spaces + size > room:
            # A word wider than the row (a run of wide characters with no
            # space) breaks at the column, one pass over it.
            cuts = cut_points(word, room - self._row_used - self._spaces, room, words=False)
            start = 0
            for end in cuts[:-1]:
                out += " " * self._spaces + self._paint(word[start:end]) + "\n" + " " * indent
                start, self._row_used, self._spaces = end, 0, 0
            word = word[start:]
            size = cells(word)
        out += " " * self._spaces + self._paint(word)
        self._row_used += self._spaces + size
        self._spaces, self._word, self._word_cells = 0, "", 0
        return out

    def chunk(self, field: str, text: str) -> None:
        if not text:
            return
        if field != self.field:
            self.finish()
            self.field = field
        cleaned = self._cleaner.feed(text)
        if self.live is None:
            if cleaned:
                self._write(self._hang_plain(cleaned))
                self._last_char = cleaned[-1]
                self._wrote = True
                self._flush()
            return
        self._source += cleaned
        wait = self._refreshed + self.refresh_s - time.monotonic()
        if wait <= 0:
            self._cancel_trailing()
            self._refresh()
            self._refreshed = time.monotonic()
        elif self._trailing is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            self._trailing = loop.call_later(wait, self._draw_held_back)

    def _draw_held_back(self) -> None:
        """Draw the text the last redraw did not include. The model may go on
        to stream a tool call for minutes with no further text chunk. Commits
        nothing: a finished block is committed from `chunk` or `finish`, which
        the caller has wrapped."""
        self._trailing = None
        if self.live is not None:
            self._draw_live(self._source)
            self._refreshed = time.monotonic()

    def _cancel_trailing(self) -> None:
        if self._trailing is not None:
            self._trailing.cancel()
            self._trailing = None

    def _commit(self, source: str) -> None:
        assert self.live is not None
        rendered = self._hung(render_markdown(source, self._width()), keep=True)
        if self._committed_any:
            rendered = "\n" + rendered
        self.live.commit(rendered)
        self._committed_any = True

    def _refresh(self) -> None:
        assert self.live is not None
        value = self._source
        starts = _block_starts(value)
        if len(starts) > 1:
            # markdown-it numbers lines by "\n" only; splitlines() would also
            # split on U+2028/U+2029, which clean() keeps, and cut mid-block.
            lines = value.split("\n")
            cut = starts[-1]
            self._commit("\n".join(lines[:cut]) + "\n")
            value = "\n".join(lines[cut:])
            self._source = value
        self._draw_live(value)

    def _draw_live(self, value: str) -> None:
        assert self.live is not None
        if value.strip():
            separator = "\n" if self._committed_any else ""
            self.live.update(separator + self._hung(render_markdown(value, self._width()), keep=False))

    def finish(self) -> None:
        """Commit whatever is open and end the stream on a fresh line."""
        self._cancel_trailing()
        held = self._cleaner.flush()  # the text of a string sequence never ended
        if held and self.live is not None:
            self._source += held
        elif held:
            self._write(self._hang_plain(held))
            self._last_char, self._wrote = held[-1], True
        if self.live is not None:
            value = self._source
            if value.strip():
                self._commit(value)
            self._source = ""
            self._committed_any = False
        elif self._wrote:
            held = self._flush_word() + self._held_spaces() if self._speaker is not None else ""
            if held:
                self._write(held)
            if self._last_char != "\n":
                self._write("\n")
            self._flush()
        self._wrote = False
        self._last_char = "\n"
        self.field = None


def print_answer(text: str, stream: Any, *, markdown: bool = True) -> None:
    """Write one finished answer: rendered Markdown on a terminal, else the
    cleaned text and a newline."""
    display = Display.for_stream(stream, markdown=markdown)
    display.chunk("text", text)
    display.finish()


# --------------------------------------------------------------------------
# Tool exchanges
# --------------------------------------------------------------------------

# The argument each tool carries its program or file body in, shown as a
# highlighted block under the call header instead of on it.
_BODY_ARG: dict[str, str] = {
    "shell": "command",
    "terminal_session": "command",
    "kernel": "code",
    "toolbox": "source",
    "write": "content",
    "plan": "content",
}
# Arguments the header leaves out: the body, or text the result's diff shows.
_UNLISTED_ARGS: dict[str, tuple[str, ...]] = {
    "patch": ("old_string", "new_string", "edits"),
    "read": ("range",),
    "shell": ("description",),
    "send": ("text",),
    "post_task": ("body",),
    "finish_task": ("result",),
}
_SHELL_HEADER = re.compile(r"shell=[^\n]*\nexit=(-?\d+)\n")
_SHELL_SECTION = re.compile(r"^--- (stdout|stderr) ---$")
_PYTHON_FIRST_LINE = re.compile(r"^\s*(?:\S*/)?python[0-9.]*\b")
# A `read` result line: `N|` before the file's own text.
_READ_GUTTER = re.compile(r"^(\d+)\|")
# The note a paged `read` ends with, written for the model.
_READ_PAGING = re.compile(r"^\[(\d+) total lines; continue with .*\]$")
_PATCHED = re.compile(r"^patched .*?\((.*), hash \w+\)$")
_HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")

# Screen marks of an exchange. `MARK` opens a call that has not finished;
# a finished one is `OK` or `FAIL`.
MARK = "…"
OK = "✓"
FAIL = "✗"
# The bar down the left edge of an exchange, in the colour of how it stands.
RAIL = "▌"
_RAIL = 2
_SGR = re.compile(r"\x1b\[[0-9;]*m")
_RAILED = re.compile(rf"^{RAIL} ?", re.M)
_HEADER_MARK = re.compile(rf"^[{MARK}{OK}{FAIL}] +", re.M)  # the mark and the chip's padding
# Columns a body sits in from the rail, and a panel's inner padding.
_INDENT = 1
_PAD = 1
# Columns a wrapped header row sits in from the rail: past the mark.
_HANG = 2


@dataclass(frozen=True)
class Theme:
    """The colours of a tool exchange and an answer, each an /fset colour entry."""

    accent: str      # tool name, running mark
    highlight: str   # Markdown headings, links, list numbers
    ok: str          # finished mark
    error: str       # failed mark, error text
    warn: str        # stderr
    dim: str         # arguments, line numbers, metrics
    panel: str       # output panel background
    panel_text: str  # output panel text
    added: str       # diff: added line
    removed: str     # diff: removed line
    code: str        # inline code in an answer
    separator: str   # rules and table borders


_THEME_ROLES = {
    "accent": "COLOR_TOOL", "highlight": "COLOR_ACCENT", "ok": "COLOR_OK", "error": "COLOR_ERROR", "warn": "COLOR_WARN",
    "dim": "COLOR_DIM", "panel": "COLOR_PANEL", "panel_text": "COLOR_TOOL_OUTPUT",
    "added": "COLOR_ADDED", "removed": "COLOR_REMOVED", "code": "COLOR_CODE", "separator": "COLOR_SEPARATOR",
}


def theme(settings: Any = None) -> Theme:
    """The colours /fset gives now; ``settings`` is unused, kept for callers."""
    return Theme(**{field: fset.hex_for(role) or "default" for field, role in _THEME_ROLES.items()})


def for_transcript(rendered: str) -> str:
    """`rendered` as the transcript log records it: plain text without the
    rail, every exchange header opening with `TOOL_MARKER` whatever mark the
    screen drew."""
    rail = re.compile(rf"^{re.escape(fset.setting_value('EXCHANGE_RAIL'))} ?", re.M)
    marks = "|".join(re.escape(fset.setting_value(key)) for key in ("EXCHANGE_MARK_RUNNING", "EXCHANGE_MARK_OK", "EXCHANGE_MARK_FAILED"))
    header = re.compile(rf"^(?:{marks}) +", re.M)
    return header.sub(f"{TOOL_MARKER} ", rail.sub("", _SGR.sub("", rendered)))


@dataclass
class ToolOutput:
    """A tool result as the screen shows it: cleaned lines with their stream."""

    lines: list[tuple[str, bool]]  # (text, is_stderr)
    exit_code: int | None = None
    is_error: bool = False
    tries: tuple[int, int] | None = None  # (left, allowed) of a failed call

    @property
    def text(self) -> str:
        return "\n".join(line for line, _ in self.lines)

    @property
    def failed(self) -> bool:
        return self.is_error or bool(self.exit_code)


def result_text(result: Any) -> tuple[str, bool]:
    """Flatten a tool result to (text, is_error)."""
    dehydrated = getattr(result, "dehydrated", None)
    if callable(dehydrated):
        return dehydrated(), bool(getattr(result, "is_error", False))
    text = result if isinstance(result, str) else str(result)
    return text, text.lstrip().startswith("ERROR")


# The count `runtime.ToolErrorTracker` appends to a failed result for the model.
_RETRY_NOTE = re.compile(r"\s*<retry>attempts_left=(\d+), allowed_max_attempts=(\d+)</retry>\s*$")


def tool_output(name: str, result: Any) -> ToolOutput:
    """Parse a result for display. A finished shell result loses its
    `shell=… exit=… --- stdout ---` envelope; exit is kept apart. A failed
    call's retry note leaves the text and becomes `tries`."""
    text, is_error = result_text(result)
    text = clean(text)
    tries: tuple[int, int] | None = None
    note = _RETRY_NOTE.search(text)
    if note is not None:
        tries = (int(note.group(1)), int(note.group(2)))
        text = text[:note.start()]
    if name == "shell":
        header = _SHELL_HEADER.match(text)
        if header is not None:
            exit_code = int(header.group(1))
            lines: list[tuple[str, bool]] = []
            stream: str | None = None
            for line in text.split("\n"):
                section = _SHELL_SECTION.match(line)
                if section is not None:
                    stream = section.group(1)
                    continue
                if stream is not None:
                    lines.append((line, stream == "stderr"))
            while lines and not lines[-1][0]:
                lines.pop()
            return ToolOutput(lines, exit_code=exit_code or None, is_error=is_error)
    body = text.rstrip("\n")
    return ToolOutput([(line, False) for line in body.split("\n")] if body else [],
                      is_error=is_error, tries=tries)


def _byte_len(text: str) -> int:
    return len(text.encode("utf-8", errors="replace"))


def _size(count: int) -> str:
    if count < 1024:
        return f"{count} B"
    if count < 1024 * 1024:
        return f"{count / 1024:.1f} KB"
    return f"{count / (1024 * 1024):.1f} MB"


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def metrics(output: ToolOutput, *, shown: int | None = None, lines: int | None = None,
            of: int | None = None) -> str:
    """`[failed · L of N tries left · ][exit N · ]{lines} lines[ of {of}] · {size}`,
    plain text. `shown` is how many of the lines are on screen when some are
    hidden; `lines` overrides the result's own line count and `of` is the
    file's, for a `read` of one page."""
    parts: list[str] = []
    if output.tries is not None:
        parts.append(f"failed · {output.tries[0]} of {output.tries[1]} tries left")
    if output.exit_code:
        parts.append(f"exit {output.exit_code}")
    total = len(output.lines) if lines is None else lines
    if of is not None and of > total:
        parts.append(f"{total} of {_count(of, 'line')}")
    else:
        parts.append(_count(total, "line"))
    if shown is not None and shown < total:
        parts.append(f"{shown} shown")
    parts.append(_size(_byte_len(output.text)))
    return " · ".join(parts)


# The argument that tells one call of a tool from another on its header: the
# first of these that the call carries. A tool not named here is told apart by
# its path, when it has one.
_KEY_ARGS: dict[str, tuple[str, ...]] = {
    "fs_search": ("pattern",),
    "ast_search": ("pattern",),
    "shell": ("command",),
    "terminal_session": ("command",),
    "kernel": ("code",),
    "toolbox": ("source",),
    "fetch": ("url",),
    "browse": ("url",),
    "exa_search": ("query",),
    "serper_search": ("query",),
    "tavily_search": ("query",),
    "docs_search": ("topic",),
    "tool_discovery": ("query",),
    "skill": ("name",),
    "send": ("to",),
    "claim": ("key",),
    "release": ("key",),
    "plan": ("plan_name",),
    "post_task": ("title",),
    "task": ("agent_id",),
}
_PATH_ARGS = ("file_path", "path")


def short_path(path: str) -> str:
    """`path` under the working directory as a relative path, under the home
    directory as `~/…`, anything else unchanged. Run from the home directory
    itself, a path there is `~/…`."""
    home, cwd = os.path.expanduser("~"), os.getcwd()
    bases = ((home, "~/"),) if cwd.rstrip("/") == home.rstrip("/") else ((cwd, ""), (home, "~/"))
    for base, prefix in bases:
        if base not in ("", "/") and path.startswith(base.rstrip("/") + "/"):
            return prefix + path[len(base.rstrip("/")) + 1:]
    return path


def _key_name(name: str, args: dict | None) -> str | None:
    if not isinstance(args, dict):
        return None
    keys = _KEY_ARGS.get(name, _PATH_ARGS)
    return next((key for key in keys if isinstance(args.get(key), str) and args[key].strip()), None)


def key_argument(name: str, args: dict | None, room: int, whole: bool = False) -> str:
    """The call's key argument on one line of at most `room` cells: a path
    is shortened and keeps its tail, anything else its head; "" when the call
    has none. With ``whole`` every line of it, a newline shown as ⏎."""
    key = _key_name(name, args)
    if key is None:
        return ""
    lines = clean(args[key]).strip().split("\n")  # type: ignore[index]
    if whole:
        return fset.setting_value("ARGUMENT_NEWLINE_MARK").join(short_path(line.strip()) if key in _PATH_ARGS else line.strip() for line in lines)
    text = lines[0].strip()
    more = fset.setting_value("DISPLAY_ELLIPSIS") if len(lines) > 1 else ""
    if key in _PATH_ARGS:
        text = short_path(text)
    if len(text) + len(more) <= room:
        return text + more
    if key in _PATH_ARGS:
        return fset.setting_value("DISPLAY_ELLIPSIS") + text[-(room - 1):]
    return text[:room - 1] + fset.setting_value("DISPLAY_ELLIPSIS")


def _single_line(value: Any, whole: bool = False) -> str:
    """An argument's value on one line: its head, cut at 40 columns, or with
    ``whole`` all of it, a newline shown as ⏎."""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(value)
    text = clean(text)
    if whole:
        return text.replace("\n", fset.setting_value("ARGUMENT_NEWLINE_MARK"))
    first, _, rest = text.partition("\n")
    maximum = int(fset.setting_value("ARGUMENT_CHARACTERS_MAX"))
    if rest or len(first) > maximum:
        first = first[:maximum - 1] + fset.setting_value("DISPLAY_ELLIPSIS")
    return first


def _other_arguments(name: str, args: dict, whole: bool = False) -> str:
    """What the header shows after the key argument, dim: a `read`'s range, a
    `write`'s length, a `patch`'s edit count, then every remaining argument as
    `(key=value, …)`."""
    notes: list[str] = []
    span = args.get("range")
    if name == "read" and isinstance(span, dict):
        span = {key: clean(value) if isinstance(value, str) else value for key, value in span.items()}
        args = {**args, "range": span}
        if "start_byte" in span or "end_byte" in span:
            notes.append(f"bytes {span.get('start_byte', 0)}-{span.get('end_byte', '')}")
        elif "start_line" in span or "end_line" in span:
            notes.append(f"lines {span.get('start_line', 1)}-{span.get('end_line', '')}")
    if name == "write" and isinstance(args.get("content"), str):
        notes.append(_count(len(args["content"].rstrip("\n").split("\n")), "line"))
    if name == "patch" and isinstance(args.get("edits"), list):
        notes.append(_count(len(args["edits"]), "edit"))
    # The whole form leaves out only what it shows elsewhere: the key
    # argument, the body, a message's text.
    shown_elsewhere = (_SAID_ARG.get(name),) if whole else _UNLISTED_ARGS.get(name, ())
    left_out = {_key_name(name, args), _BODY_ARG.get(name), *shown_elsewhere}
    rest = [f"{clean(str(key))}={_single_line(value, whole)}" for key, value in args.items()
            if key not in left_out and value is not None and value != ""]
    if rest:
        notes.append("(" + ", ".join(rest) + ")")
    return clean(" ".join(notes))


def _lexer(name: str, args: dict, body: str) -> str:
    if name in ("kernel", "toolbox"):
        return "python"
    if name in ("shell", "terminal_session"):
        first = body.lstrip().split("\n", 1)[0]
        return "python" if _PYTHON_FIRST_LINE.match(first) else "bash"
    if name == "plan":
        return "markdown"
    path = args.get("file_path") or args.get("path")
    if isinstance(path, str) and path:
        try:
            return Syntax.guess_lexer(path, body)
        except Exception:  # noqa: BLE001 - pygments lookup on an odd path
            return "text"
    return "text"


def _highlight(lines: list[str], lexer: str) -> list[Text]:
    """Each line as `lexer` source, one Text per input line."""
    if lexer in ("text", "default"):
        return [Text(line) for line in lines]
    code = "\n".join(lines)
    parts = list(Syntax(code, lexer, theme=fset.code_style(), background_color="default")
                 .highlight(code).split("\n", allow_blank=True))[:len(lines)]
    return parts if len(parts) == len(lines) else [Text(line) for line in lines]


def _fit(text: Text, room: int) -> list[Text]:
    """`text` as rows of at most `room` cells: a long line breaks at the
    column, as a terminal wraps it, and keeps all of itself."""
    text = text.copy()
    text.expand_tabs(TAB)
    cuts = cut_points(text.plain, room, words=False)[:-1]
    return list(text.divide(cuts)) if cuts else [text]




class _Block:
    """The rows of one exchange, drawn at `width` behind a rail in the colour
    of `state`: running, finished or failed."""

    def __init__(self, width: int, colours: Theme, state: str = MARK) -> None:
        self.width = max(width, 10)
        self.inner = self.width - _RAIL
        self.theme = colours
        self.state = state
        self.rows: list[Text] = []
        self.whole = False  # TOOLS_DISPLAY 3: the header shows every argument whole

    def colour(self) -> str:
        return {MARK: self.theme.warn, OK: self.theme.ok, FAIL: self.theme.error}[self.state]

    def header(self, name: str, args: dict | None, *, malformed: bool = False,
               about: str | None = None, rest: str | None = None,
               tail: str | list[fset.Run] = "", tail_style: str = "") -> None:
        """`mark [name] key-argument  other-arguments`, the name in COLOR_TOOL
        on the panel gray; a row too long wraps under the name, nothing is
        cut. `tail`, the call's last word, sits on its own row below, behind
        TOOL_RESULT_LEAD. `about` and `rest` replace what the arguments would
        give."""
        th = self.theme
        args = args if isinstance(args, dict) else {}
        mark = fset.setting_value({MARK: "EXCHANGE_MARK_RUNNING", OK: "EXCHANGE_MARK_OK", FAIL: "EXCHANGE_MARK_FAILED"}[self.state])
        style = fset.setting_value("EXCHANGE_HEADER_STYLE")
        row = Text.assemble((f"{mark} ", f"{style} {self.colour()}"),
                            (f" {clean(name)} ", f"{style} {th.accent} on {th.panel}"))
        if malformed:
            row.append_text(Text(" ") + fset.to_rich(msgs.TOOL_ARGS_UNPARSED.body_runs(), th.error))
        if about is None:
            # A program shown whole in its own panel keeps only its first line here.
            whole = self.whole and _BODY_ARG.get(name) != _key_name(name, args)
            about = key_argument(name, args, 10 ** 6, whole)
        if about:
            row.append(" " + about, fset.setting_value("EXCHANGE_ARGUMENT_STYLE"))
        if rest is None:
            rest = _other_arguments(name, args, self.whole)
        if rest:
            row.append("  " + rest, th.dim)
        self._hanging(row)
        if tail:
            self._hanging(fset.to_rich(msgs.TOOL_RESULT_LEAD.body_runs(), th.dim) + Text(" ")
                          + _piece(tail, tail_style or th.dim),
                          lead=_HANG)

    def _hanging(self, text: Text, lead: int = 0) -> None:
        """``text`` as rows: the first ``lead`` columns in, the rest under the
        text that follows the mark."""
        text = text.copy()
        text.expand_tabs(TAB)
        cuts = cut_points(text.plain, self.inner - lead, self.inner - _HANG - lead)
        pieces = text.divide(cuts[:-1]) if text.plain else []
        for index, piece in enumerate(pieces):
            if index:
                # The spaces at a break are the break: they start no row.
                piece = piece[len(piece.plain) - len(piece.plain.lstrip(" ")):]
                if not piece.plain:
                    continue
            if index < len(pieces) - 1:
                piece.rstrip()
            self.rows.append(Text(" " * (lead if index == 0 else _HANG + lead)) + piece)

    def _panel_row(self, piece: Text) -> None:
        row = Text(" " * _PAD, style=f"{self.theme.panel_text} on {self.theme.panel}")
        row.append_text(piece)
        row.pad_right(self.inner - _INDENT - cells(row.plain))
        self.rows.append(Text(" " * _INDENT) + row)

    def panel(self, lines: list[Text]) -> None:
        """`lines` on the panel background, padded to one width."""
        room = self.inner - _INDENT - 2 * _PAD
        for line in lines:
            for piece in _fit(line, room):
                self._panel_row(piece)

    def numbered(self, lines: list[tuple[str, Text]], *, sign: int = 0) -> None:
        """`(number, text)` rows as a file view on the panel: dim
        right-aligned numbers, the text beside them, a folded row continuing
        under its text. The first `sign` columns of a text, a diff's `+ ` or
        `- `, stand alone in their column: a folded row continues after them."""
        gutter = max((len(number) for number, _ in lines), default=1)
        room = self.inner - _INDENT - 2 * _PAD - gutter - 2 - sign
        for number, text in lines:
            head = text[:sign]
            for index, piece in enumerate(_fit(text[sign:], room)):
                label = number.rjust(gutter) if index == 0 else " " * gutter
                lead = head if index == 0 else Text(" " * sign)
                self._panel_row(Text.assemble((label, self.theme.dim), "  ", lead, piece))

    def note(self, text: str | list[fset.Run], style: str = "") -> None:
        """A dim row of `text`, plain text or a message's colour runs,
        wrapped under itself when it is long."""
        self._hanging(_piece(text, style or self.theme.dim), lead=_INDENT + _PAD)

    def render(self) -> str:
        if not self.rows:
            return ""
        console = _render_console(self.width)
        rail = Text.assemble((fset.setting_value("EXCHANGE_RAIL") + " ", self.colour()))
        for row in self.rows:
            console.print(rail + row, soft_wrap=True)
        return console.file.getvalue()  # type: ignore[attr-defined]


def _piece(text: str | list[fset.Run], style: str) -> Text:
    """Plain text, or a message's colour runs, as rich text on ``style``."""
    return fset.to_rich(text, style) if isinstance(text, list) else Text(text, style)


def _hidden(count: int) -> list[fset.Run]:
    return msgs.TOOL_MORE_LINES.body_runs(count=count)


def _names(settings: Any, key: str) -> frozenset[str]:
    value = _setting(settings, key)
    return frozenset(str(item) for item in value) if isinstance(value, (list, tuple)) else frozenset()


_SHELL_JOIN = re.compile(r"\s*(?:&&|\|\||;|\|)\s*")


def _looks_around(command: Any, settings: Any) -> bool:
    """A shell command made only of SHELL_QUIET commands, writing nowhere."""
    if not isinstance(command, str) or not command.strip() or "\n" in command.strip() or ">" in command:
        return False
    quiet = _names(settings, "SHELL_QUIET")
    for part in _SHELL_JOIN.split(command.strip()):
        words = part.split()
        if not words or (words[0] not in quiet and " ".join(words[:2]) not in quiet):
            return False
    return True


def call_kind(name: str, args: dict | None, settings: Any = None) -> str:
    """What TOOLS_DISPLAY 1 shows of a call.

    quiet    one row, and a run of them folds into one row per tool
    command  its output while it runs, one row once it ends
    diff     the change it made
    loud     its result
    """
    args = args if isinstance(args, dict) else {}
    if name in _names(settings, "TOOLS_QUIET"):
        return "quiet"
    if name == "shell":
        return "quiet" if _looks_around(args.get("command"), settings) else "command"
    if name in ("kernel", "terminal_session", "toolbox"):
        return "command"
    if name in ("patch", "write", "notebook_edit"):
        return "diff"
    return "loud"


# What a fold row counts, singular and plural; a tool not named counts calls.
_FOLD_NOUN: dict[str, tuple[str, str]] = {
    "read": ("file", "files"),
    "fs_search": ("search", "searches"),
    "ast_search": ("search", "searches"),
    "exa_search": ("search", "searches"),
    "serper_search": ("search", "searches"),
    "tavily_search": ("search", "searches"),
    "docs_search": ("search", "searches"),
    "fetch": ("page", "pages"),
    "browse": ("page", "pages"),
    "shell": ("look", "looks"),
}
_URL_HOST = re.compile(r"^[a-z][a-z0-9+.-]*://([^/?#]+)", re.I)


def fold_label(name: str, args: dict | None) -> str:
    """One call's name on a fold row: a path's last part, a URL's host, a
    shell command's own name, anything else the head of its key argument."""
    key = _key_name(name, args)
    if key is None:
        return ""
    text = clean(args[key]).strip().split("\n", 1)[0].strip()  # type: ignore[index]
    if key in _PATH_ARGS:
        return os.path.basename(text.rstrip("/")) or text
    host = _URL_HOST.match(text)
    if host is not None:
        return host.group(1)
    if name == "shell":
        words = text.split()
        return " ".join(words[:2]) if words[:1] == ["git"] else (words[0] if words else "")
    return text if len(text) <= 24 else text[:23] + "…"


def render_fold(name: str, calls: list[dict | None], *, running: int = 0,
                width: int | None = None, settings: Any = None) -> str:
    """Finished quiet calls of one tool as one row: `mark [name] N things
    label · label · +K`. `running` of them have not returned."""
    block = _Block(width or block_width(), theme(settings), MARK if running else OK)
    one, many = _FOLD_NOUN.get(name, ("call", "calls"))
    about = f"{len(calls)} {one if len(calls) == 1 else many}"
    labels = [label for label in (fold_label(name, args) for args in calls) if label]
    room = block.inner - len(name) - len(about) - 10
    shown: list[str] = []
    for index, label in enumerate(labels):
        more = len(labels) - index - 1
        line = " · ".join([*shown, label]) + (f" · +{more}" if more else "")
        if len(line) > room and shown:
            shown.append(f"+{len(labels) - index}")
            break
        shown.append(label)
    block.header(name, None, about=about, rest=" · ".join(shown))
    return block.render()


def _read_view(output: ToolOutput, args: dict | None) -> tuple[list[tuple[str, Text]], int | None] | None:
    """A `read` result as numbered file rows and the file's total line count
    from the paging note; None when the result has no `N|` lines."""
    matches = [_READ_GUTTER.match(line) for line, _ in output.lines]
    if not any(matches):
        return None
    total: int | None = None
    kept: list[tuple[str, str]] = []
    notes: list[str] = []
    for (line, _), match in zip(output.lines, matches, strict=True):
        paging = _READ_PAGING.match(line)
        if match is not None:
            kept.append((match.group(1), line[match.end():]))
        elif paging is not None:
            total = int(paging.group(1))
        elif line.strip():
            notes.append(line)
    code = _highlight([text for _, text in kept], _lexer("read", args or {}, "\n".join(t for _, t in kept)))
    rows = [(number, text) for (number, _), text in zip(kept, code, strict=True)]
    return rows + [("", Text(note)) for note in notes], total


def _diff_view(output: ToolOutput, colours: Theme) -> tuple[list[tuple[str, Text]], str] | None:
    """A `patch` result as numbered diff rows and its summary; None when the
    result is not `patched … (summary, hash …)` followed by a unified diff."""
    if not output.lines:
        return None
    summary = _PATCHED.match(output.lines[0][0])
    if summary is None:
        return None
    rows: list[tuple[str, Text]] = []
    old = new = 0
    for line, _ in output.lines[1:]:
        hunk = _HUNK.match(line)
        if hunk is not None:
            if rows:
                rows.append(("", Text(fset.setting_value("DIFF_HUNK_GAP"), colours.dim)))
            old, new = int(hunk.group(1)), int(hunk.group(2))
        elif line.startswith(("--- ", "+++ ")) and not old and not new:
            continue
        elif line.startswith("+"):
            rows.append((str(new), Text("+ " + line[1:], colours.added)))
            new += 1
        elif line.startswith("-"):
            rows.append((str(old), Text("- " + line[1:], colours.removed)))
            old += 1
        elif line.startswith(" "):
            rows.append((str(new), Text("  " + line[1:], colours.panel_text)))
            old, new = old + 1, new + 1
    return rows, summary.group(1)


# The argument a call's own text is in, shown at TOOLS_DISPLAY 1 in place of the
# result, which only acknowledges it.
_SAID_ARG: dict[str, str] = {"send": "text", "post_task": "body", "finish_task": "result"}
# Rows of a running command's output kept on screen.


def _last_line(output: ToolOutput) -> str:
    return next((line.strip() for line, _ in reversed(output.lines) if line.strip()), "")


def _program(block: _Block, name: str, args: dict, level: int, preview: int) -> None:
    """The call's program under its header at TOOLS_DISPLAY 2 and 3: a `write`'s
    file as a numbered view, any other body of more than one line as a panel."""
    body = args.get(_BODY_ARG.get(name, ""))
    if not isinstance(body, str) or not body.strip():
        return
    lines = clean(body).rstrip("\n").split("\n")
    if name != "write" and len(lines) == 1 and _key_name(name, args) == _BODY_ARG.get(name):
        # A one-line program is the header's key argument already.
        return
    hidden = len(lines) - preview if level == 2 and len(lines) > preview else 0
    lines = lines[:len(lines) - hidden]
    code = _highlight(lines, _lexer(name, args, "\n".join(lines)))
    if name == "write":
        block.numbered([(str(n), text) for n, text in enumerate(code, 1)])
    else:
        block.panel(code)
    if hidden:
        block.note(_hidden(hidden))


def _result_rows(output: ToolOutput, colours: Theme) -> list[Text]:
    return [Text(line, colours.error if output.is_error else colours.warn if err else colours.panel_text)
            for line, err in output.lines]


def _capped(block: _Block, rows: list[Any], limit: int, draw: Callable[[list[Any]], None]) -> int:
    """Draw the first `limit` of `rows` and a note of how many are hidden."""
    draw(rows[:limit])
    if len(rows) > limit:
        block.note(_hidden(len(rows) - limit))
    return min(limit, len(rows))


def _finished_brief(block: _Block, name: str, args: dict, output: ToolOutput, kind: str,
                    preview: int, malformed: bool) -> None:
    """A finished exchange at TOOLS_DISPLAY 1."""
    colours = block.theme
    command = kind == "command" or name == "shell"
    if output.failed:
        if command:
            parts = [f"exit {output.exit_code}"] if output.exit_code else []
            last = _last_line(output)
            block.header(name, args, malformed=malformed,
                         tail=" · ".join([*parts, last] if last else parts), tail_style=colours.error)
            return
        if kind == "quiet" and len(output.lines) == 1:
            block.header(name, args, malformed=malformed, tail=output.lines[0][0].strip(),
                         tail_style=colours.error)
            return
        block.header(name, args, malformed=malformed)
        _capped(block, _result_rows(output, colours), preview, lambda rows: block.panel(rows))
        if output.tries is not None:
            block.note(msgs.TOOL_TRIES_LEFT.body_runs(left=output.tries[0], allowed=output.tries[1]), colours.error)
        return
    if kind == "quiet":
        block.header(name, args, malformed=malformed)
        return
    if command:
        block.header(name, args, malformed=malformed, tail=_last_line(output))
        return
    diff = _diff_view(output, colours) if name == "patch" else None
    if diff is not None:
        rows, lead = diff
        block.header(name, args, malformed=malformed, tail=lead)
        _capped(block, rows, preview, lambda shown: block.numbered(shown, sign=2))
        return
    said = args.get(_SAID_ARG.get(name, ""))
    if isinstance(said, str) and said.strip():
        block.header(name, args, malformed=malformed, tail=_last_line(output))
        lines = [Text(line, colours.panel_text) for line in clean(said).strip("\n").split("\n")]
        _capped(block, lines, preview, lambda rows: block.panel(rows))
        return
    if kind == "diff" or len(output.lines) <= 1:
        block.header(name, args, malformed=malformed, tail=_last_line(output))
        return
    block.header(name, args, malformed=malformed)
    _capped(block, _result_rows(output, colours), preview, lambda rows: block.panel(rows))


def _finished_full(block: _Block, name: str, args: dict, output: ToolOutput, level: int,
                   preview: int, malformed: bool) -> None:
    """A finished exchange at TOOLS_DISPLAY 2 and 3: the program, the result as
    a file view, a diff or a panel, and its metrics."""
    colours = block.theme
    block.header(name, args, malformed=malformed)
    _program(block, name, args, level, preview)
    said = args.get(_SAID_ARG.get(name, ""))
    if isinstance(said, str) and said.strip():
        # What the call itself said, which TOOLS_DISPLAY 1 shows in place of the result.
        lines = [Text(line, colours.panel_text) for line in clean(said).strip("\n").split("\n")]
        _capped(block, lines, preview if level == 2 else len(lines), lambda rows: block.panel(rows))
    limit = preview if level == 2 else max(len(output.lines), 1)
    note_style = colours.error if output.failed else ""
    if not output.lines:
        block.note(metrics(output) if output.failed else msgs.TOOL_NO_OUTPUT.body_runs(), note_style)
        return
    read = _read_view(output, args) if name == "read" else None
    diff = _diff_view(output, colours) if name == "patch" else None
    if read is not None:
        rows, of = read
        shown = _capped(block, rows, limit, lambda part: block.numbered(part))
        block.note(metrics(output, shown=shown, lines=len(rows), of=of), note_style)
    elif diff is not None:
        rows, lead = diff
        _capped(block, rows, limit, lambda part: block.numbered(part, sign=2))
        block.note(lead, note_style)
    else:
        shown = _capped(block, _result_rows(output, colours), limit, lambda part: block.panel(part))
        block.note(metrics(output, shown=shown), note_style)


def render_exchange(name: str, args: dict | None, result: Any = None, *, level: int,
                    preview: int = 12, width: int | None = None, malformed: bool = False,
                    settings: Any = None, live: list[str] | None = None, elapsed: str = "") -> str:
    """One tool exchange behind its rail. `result` None is a call still
    running: `live` is the tail of its output so far, `elapsed` how long it
    has run.

    TOOLS_DISPLAY  1  what `call_kind` says of the call: a quiet call is one
                      row, a command its last line, a patch its diff, anything
                      else its result; a failure shows its error
                   2  the program and the first `preview` result lines
                   3  the program and every result line
    A long line wraps; none is cut.
    """
    if level <= 0:
        return ""
    colours = theme(settings)
    args = args if isinstance(args, dict) else {}
    if result is None:
        block = _Block(width or block_width(), colours, MARK)
        block.whole = level >= 3
        block.header(name, args, malformed=malformed,
                     tail=msgs.TOOL_RUNNING.body_runs(elapsed=elapsed) if elapsed else "")
        if level >= 2:
            _program(block, name, args, level, preview)
        if live:
            tail = [clean(line) for line in live][-int(_setting(settings, "SHELL_LIVE_LINES_MAX")):]
            block.panel([Text(line, colours.panel_text) for line in tail])
        return block.render()
    output = tool_output(name, result)
    block = _Block(width or block_width(), colours, FAIL if output.failed else OK)
    block.whole = level >= 3
    if level == 1:
        _finished_brief(block, name, args, output, call_kind(name, args, settings), preview, malformed)
    else:
        _finished_full(block, name, args, output, level, preview, malformed)
    return block.render()


# --------------------------------------------------------------------------
# A resumed session's last exchanges
# --------------------------------------------------------------------------


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return "" if content is None else str(content)
    return "\n".join(
        part["text"] for part in content
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    )


def _call_args(call: dict) -> tuple[str, dict]:
    function = call.get("function") or {}
    raw = function.get("arguments")
    try:
        args = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        args = None
    return str(function.get("name") or "?"), args if isinstance(args, dict) else {}


def resume_parts(messages: list[dict], count: int, *, width: int,
                 markdown: bool = True) -> Iterator[tuple[str, Any]]:
    """The last `count` exchanges of `messages` in the order a turn drew
    them: ("text", rendered) for the user's line (`user_line`) and the answer
    hung off the agent's mark as Markdown at `width` (plain text when
    `markdown` is off), ("call", (name, args, result)) for each tool call,
    result None when none came back. An exchange starts at a user message
    that was not steered into a turn."""
    starts = [i for i, message in enumerate(messages)
              if message.get("role") == "user" and not message.get("steered")]
    if count <= 0 or not starts:
        return
    tail = messages[starts[-min(count, len(starts))]:]
    results = {message.get("tool_call_id"): message for message in tail
               if message.get("role") == "tool" and message.get("tool_call_id")}
    for message in tail:
        role = message.get("role")
        text = clean(_content_text(message.get("content"))).strip("\n")
        if role == "user":
            yield "text", user_line(text)
        elif role == "assistant":
            if text.strip():
                mark, indent = agent_speaker()
                body = render_markdown(text, width - indent) if markdown else wrap_words(text, width - indent) + "\n"
                yield "text", hang(body, mark, indent)
            for call in message.get("tool_calls") or []:
                name, args = _call_args(call)
                result = results.get(call.get("id"))
                yield "call", (name, args, None if result is None else _content_text(result.get("content")))


def render_exchanges(messages: list[dict], count: int, *, width: int,
                     level: int, preview: int, markdown: bool = True,
                     settings: Any = None) -> str:
    """`resume_parts` as one string, each tool exchange at TOOLS_DISPLAY
    `level` on its own."""
    out: list[str] = []
    for kind, part in resume_parts(messages, count, width=width, markdown=markdown):
        if kind == "text":
            out.append(part)
        else:
            name, args, result = part
            out.append(render_exchange(name, args, result, level=level, preview=preview, width=width,
                                       settings=settings))
    return "".join(out)
