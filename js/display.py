"""Model text on its way to a screen: the marks the conversation's two voices
hang off, Markdown rendered for a terminal, and the stream an answer is drawn
through.

`Display` owns one stream of answer text. On a terminal it renders Markdown:
finished top-level blocks commit once and are never redrawn; only the last,
still-open block is live. Anywhere else it writes the cleaned text as it
arrives.

`Theme` holds the colours an answer and a tool exchange (`js.tool_display`)
are drawn in, each one of /fset's `COLOR_*` entries (`js.fset`).
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import shutil
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from markdown_it import MarkdownIt
from markdown_it.token import Token
from prompt_toolkit.utils import get_cwidth
from rich.console import Console
from rich.markdown import Markdown, TableElement
from rich.text import Text
from rich.theme import Theme as _RichTheme

from . import fset, output, settings as _settings
from .control_sequences import SGR, StreamCleaner, clean
from .text_layout import TAB, block_width, cells, cut_points, terminal_width, wrap_words


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
        plain = SGR.sub("", row)
        if not plain:
            continue
        if first and plain.strip():
            rows[index], first = mark + row, False
        else:
            rows[index] = " " * indent + row
    return "\n".join(rows)


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


def setting(settings: Any, key: str) -> Any:
    """Knob ``key`` from live settings; its js/jsrc value when absent."""
    return _settings.knob(settings if isinstance(settings, dict) else None, key)


def markdown_enabled(settings: Any) -> bool:
    return setting(settings, "MARKDOWN") is not False


def render_console(width: int, styles: dict[str, str] | None = None) -> Console:
    """A console that renders into a string at ``width``: 24-bit colour, none
    under NO_COLOR."""
    return Console(
        theme=_RichTheme(styles) if styles else None,
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
    console = render_console(max(cells(text.plain), 1) + 8)
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
    """rich's Markdown with tables that wrap their cells, drawing every
    character of inline source: emphasis keeps its `*`, `_` and `~~` markers
    inside the style, a backslash escape or an entity is drawn as written, and
    HTML is text. Commands copied off the screen match what the model wrote."""

    elements = {**Markdown.elements, "table_open": _FoldingTable}

    def __init__(self, markup: str, **options: Any) -> None:
        super().__init__("", **options)
        self.markup = markup
        self.parsed = _SOURCE_PARSER.parse(markup)
        for token in self.parsed:
            if token.children:
                token.children = _with_source(token.children)


# commonmark with HTML off, so a tag is text; text_join off, so an escape or
# an entity stays a text_special token that still holds its source.
_SOURCE_PARSER = (MarkdownIt("commonmark", {"html": False})
                  .enable("strikethrough").enable("table").disable("text_join"))

_MARKER_OPEN = {"em_open", "strong_open", "s_open"}
_MARKER_CLOSE = {"em_close", "strong_close", "s_close"}


def _with_source(children: list[Token]) -> list[Token]:
    """Inline tokens with the source markdown-it consumed put back as text."""
    kept: list[Token] = []
    for child in children:
        if child.type == "text_special":
            child.type, child.content = "text", child.markup
        if child.type in _MARKER_CLOSE:
            kept.append(Token("text", "", 0, content=child.markup))
        kept.append(child)
        if child.type in _MARKER_OPEN:
            kept.append(Token("text", "", 0, content=child.markup))
    return kept


def render_markdown(source: str, width: int) -> str:
    """ANSI rendering of Markdown source at `width` columns: no blank lines
    before or after, ending in one newline. Inline code is a chip: its text in
    COLOR_CODE on COLOR_PANEL, a space of padding each side."""
    console = render_console(width, _markdown_styles(theme()))
    document = _Markdown(source, code_theme=fset.code_style(), hyperlinks=False)
    for token in document.parsed:
        for child in token.children or ():
            if child.type == "code_inline":
                padding = setting(None, "MARKDOWN_INLINE_PADDING")
                child.content = f"{padding}{child.content}{padding}"
            elif child.type == "softbreak" and setting(None, "MARKDOWN_HARD_BREAKS"):
                child.type = "hardbreak"
    console.print(document)
    return _rendered_text(console, width).strip("\n") + "\n"


def _block_starts(source: str) -> list[int]:
    """Source line numbers where each top-level Markdown block begins."""
    return [token.map[0] for token in _SOURCE_PARSER.parse(source) if token.level == 0 and token.map]


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
        if keep and SGR.sub("", rendered).strip():
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
                out += " " * self._spaces + word[start:end] + "\n" + " " * indent
                start, self._row_used, self._spaces = end, 0, 0
            word = word[start:]
            size = cells(word)
        out += " " * self._spaces + word
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


def blocking_stdio() -> None:
    """Clear O_NONBLOCK on stdin, stdout and stderr. Every program in a
    terminal shares one open file description, and one that set it
    non-blocking (nvim and node do, through libuv) can leave it so; a write
    bigger than the terminal's buffer then stops part way with EAGAIN."""
    for fd in (0, 1, 2):
        with contextlib.suppress(OSError):
            if not os.get_blocking(fd):
                os.set_blocking(fd, True)


def print_answer(text: str, stream: Any, *, markdown: bool = True) -> None:
    """Write one finished answer: rendered Markdown on a terminal, else the
    cleaned text and a newline."""
    display = Display.for_stream(stream, markdown=markdown)
    display.chunk("text", text)
    display.finish()


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


def theme() -> Theme:
    """The colours /fset gives now."""
    return Theme(**{field: fset.hex_for(role) or "default" for field, role in _THEME_ROLES.items()})
