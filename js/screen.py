"""Three-region screen for the async REPL: scrollback, status bar, input line.

Output never touches the input line because it is a different window. Every
`sys.stdout`/`sys.stderr` write during the REPL is marshalled onto the loop and
appended to the scrollback buffer; the input buffer is untouched.
"""
from __future__ import annotations

import asyncio
import functools
import os
import re
import subprocess
import sys
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass

from prompt_toolkit.application import Application, get_app, run_in_terminal
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.enums import DEFAULT_BUFFER, EditingMode
from prompt_toolkit.filters import Condition, FilterOrBool, has_focus, is_searching, vi_mode, vi_navigation_mode
from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from prompt_toolkit.key_binding import KeyBindings, KeyBindingsBase, merge_key_bindings
from prompt_toolkit.key_binding.vi_state import InputMode
from prompt_toolkit.layout import ConditionalContainer, Float, FloatContainer, HSplit, Layout, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.menus import MultiColumnCompletionsMenu
from prompt_toolkit.layout.processors import HighlightIncrementalSearchProcessor
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.styles import DynamicStyle, Style
from prompt_toolkit.widgets import SearchToolbar

from . import display, fset, keys, pastes
from . import messages as msgs
from . import session_store
from .reasoning_display import grey, settled_rows

TICK_BUSY_S = 0.1
TICK_IDLE_S = 1.0

# Variation selectors: prompt_toolkit folds one into the cell before it and
# keeps that cell's width, so U+26A0 U+FE0F (a warning sign) is one column in
# its screen model while a terminal that honours U+FE0F draws it two wide. The
# rest of that row then sits one column right of where the renderer believes
# it is, and a later repaint, which writes only the cells it thinks changed,
# leaves that row's characters showing through the next text drawn there.
# Dropped from all scrollback text; the base glyph stays, and prompt_toolkit
# and the terminal give it the same width.
_VARIATION_SELECTORS = re.compile("[\uFE0E\uFE0F]")
# Skin-tone modifiers: the Linux console cannot draw them, and prompt_toolkit's
# width for the sequence disagrees with fbcon's. Dropped on TERM=linux.
_CONSOLE_UNDRAWABLE = re.compile("[\U0001F3FB-\U0001F3FF]")
_ON_CONSOLE = os.environ.get("TERM") == "linux"


def _drawable(text: str) -> str:
    """`text` as the scrollback holds it: no variation selectors, and no
    skin-tone modifiers on the Linux console."""
    text = _VARIATION_SELECTORS.sub("", text)
    return _CONSOLE_UNDRAWABLE.sub("", text) if _ON_CONSOLE else text


# --- status bar ------------------------------------------------------------

def status_style() -> str:
    """The bar's prompt_toolkit style: COLOR_STATUS_TEXT on COLOR_STATUS_BACKGROUND."""
    if not fset.colour_on():
        return "bold reverse"
    fg = fset.hex_for("COLOR_STATUS_TEXT")
    bg = fset.hex_for("COLOR_STATUS_BACKGROUND")
    return " ".join(filter(None, ("bold", fg, f"bg:{bg}" if bg else "")))


def screen_style() -> str:
    """The whole screen's prompt_toolkit style: COLOR_TEXT, on
    COLOR_BACKGROUND when the theme sets one, else on the terminal's own."""
    if not fset.colour_on():
        return ""
    bg = fset.hex_for("COLOR_BACKGROUND")
    fg = fset.hex_for("COLOR_TEXT")
    return " ".join(filter(None, (f"bg:{bg}" if bg else "", fg or "")))


def accent_clock(line: str) -> str:
    """The bar's `[HH:MM]` lead in COLOR_STATUS_ACCENT, as ANSI text."""
    end = line.find("]") + 1 if line.startswith("[") else 0
    if not end:
        return line
    code = fset.fg("COLOR_STATUS_ACCENT")
    return f"{code}{line[:end]}{fset.FG_DEFAULT}{line[end:]}" if code else line


@functools.lru_cache(maxsize=8)
def _status_style_sheet(colours: str) -> Style:
    return Style.from_dict({"status": colours})


def format_count(n: int) -> str:
    """Output tokens as a heartbeat: hundreds under 10k, then one-decimal k."""
    if n < 10_000:
        return f"{n // 100 * 100:,}"
    return f"{n / 1000:.1f}k"


def throbber_frame(now: float) -> str:
    frames = fset.setting_value("SPINNER_FRAMES")
    return frames[int(now / TICK_BUSY_S) % len(frames)] if frames else ""


def turn_centre(status, *, now: float, show_bytes: bool) -> tuple[str, int | None]:
    """(phase, output_tokens) for the bar's centre group from a TurnStatus.

    A running tool wins, then compaction, then response bytes before the first
    token (when the net channel shows them), then the output-token count.
    """
    if status.tool:
        extra = msgs.STATUS_TOOL_EXTRA.words(tool_extra=status.tool_extra) if status.tool_extra else ""
        elapsed = int(max(0.0, now - status.tool_started))
        return msgs.STATUS_TOOL_ELAPSED.words(tool=status.tool, extra=extra, elapsed=elapsed), None
    if status.compacting:
        return _field(msgs.STATUS_COMPACTING), None
    if show_bytes and status.net_bytes:
        return msgs.STATUS_RESPONSE_BYTES.words(value=status.net_bytes // 100 * 100), None
    return "", status.output_tokens or None


def _field(message: msgs.Message, **fields) -> str:
    """One status bar field through its /fset format, palette colours kept;
    a tab is one space on the bar's one row."""
    return message.line(**fields).replace("\t", " ")


def _width(text: str) -> int:
    """Columns ``text`` takes on the bar (`js.display.cells`)."""
    return display.cells(text)


def status_line(
    width: int,
    *,
    clock: str,
    provider: str | None,
    model: str | None,
    context_tokens: int | None,
    phase: str,
    output_tokens: int | None,
    throbber: str,
    agent_id: str | None,
    session_short: str | None,
    cache_pct: int | None,
    cost: str | None = None,
) -> str:
    """The status bar as a plain string of exactly `width` cells.

    Left `[HH:MM] provider/model context`, centre `throbber phase count` (only
    while `throbber` is set, i.e. a turn runs), right `agent/session cache N%
    cost`, where `cost` is the session's spend (`usage_display.status_text`). Each
    field is drawn through its /fset format, STATUS_CLOCK and the rest.
    Groups that do not fit give way in a fixed order: cache, then the model's
    head, then the provider, then the centre count, then the cost, then the
    agent id. The session name stays, elided with an ellipsis when it still
    does not fit.
    """
    if width <= 0:
        return ""
    model_text = model or ""
    session_text = session_short or ""
    show = {"cache": cache_pct is not None, "provider": bool(provider),
            "count": output_tokens is not None, "agent": bool(agent_id), "cost": bool(cost)}

    clock_text = _field(msgs.STATUS_CLOCK, clock=clock)

    def groups() -> tuple[str, str, str]:
        route = "/".join(filter(None, (provider if show["provider"] else "", model_text)))
        left = " ".join(filter(None, (
            clock_text, _field(msgs.STATUS_ROUTE, route=route) if route else "",
            "" if context_tokens is None else _field(msgs.STATUS_CONTEXT, tokens=f"{context_tokens:,}"))))
        centre = ""
        if throbber:
            count = (_field(msgs.STATUS_REPLY, tokens=format_count(output_tokens))
                     if show["count"] and output_tokens is not None else "")
            centre = " ".join(filter(None, (_field(msgs.STATUS_SPINNER, spinner=throbber),
                                            _field(msgs.STATUS_PHASE, phase=phase) if phase else "", count)))
        who = "/".join(filter(None, (agent_id if show["agent"] else "", session_text)))
        right = " ".join(filter(None, (_field(msgs.STATUS_WHO, who=who) if who else "",
                                       _field(msgs.STATUS_CACHE, pct=cache_pct) if show["cache"] else "",
                                       _field(msgs.STATUS_COST, cost=cost) if show["cost"] else "")))
        return left, centre, right

    def fits(left: str, centre: str, right: str) -> bool:
        gaps = 4 if centre else (2 if right else 0)
        return _width(left) + _width(centre) + _width(right) + gaps <= width

    def truncate_model() -> None:
        nonlocal model_text
        if len(model_text) > 12:
            model_text = "…" + model_text[-12:]

    steps = (
        lambda: show.update(cache=False),
        truncate_model,
        lambda: show.update(provider=False),
        lambda: show.update(count=False),
        lambda: show.update(cost=False),
        lambda: show.update(agent=False),
    )
    left, centre, right = groups()
    for step in steps:
        if fits(left, centre, right):
            break
        step()
        left, centre, right = groups()

    gap = width - _width(left) - _width(right)
    if centre and gap >= _width(centre) + 4:
        before = max(2, (gap - _width(centre)) // 2)
        middle = " " * before + centre
        line = left + middle + " " * (gap - _width(middle)) + right
    elif not centre and gap >= (2 if right else 0):
        line = left + " " * gap + right
    else:
        # Still too wide: keep the clock, throbber and session, cut the left group.
        room = width - _width(clock_text) - 1 - (_width(centre) + 1 if centre else 0)
        session_text = _elide(session_text, room)
        left, centre, right = (fset.plain(group) for group in groups())
        tail = " ".join(filter(None, (centre, right)))
        room = max(_width(clock_text), width - _width(tail) - 1)
        line = " ".join(filter(None, (_cut(left, room), tail)))
    if _width(line) > width:
        line = _cut(fset.plain(line), width)
    return line + " " * (width - _width(line))


def session_short(session_file) -> str:
    """The session as the bar names it: the first 8 hex chars of a generated
    `<timestamp>-<hex>` stem, else the whole stem, the name it was given."""
    stem = getattr(session_file, "stem", "") or ""
    return stem.rsplit("-", 1)[-1][:8] if session_store.is_generated(stem) else stem


def _cut(text: str, room: int) -> str:
    """The head of plain ``text`` that fits in ``room`` columns."""
    return text[:display.cut_points(text, max(room, 0), words=False)[0]] if room > 0 else ""


def _elide(text: str, room: int) -> str:
    """`text` in at most `room` cells, cut short with an ellipsis."""
    if _width(text) <= room:
        return text
    return _cut(text, room - 1) + "…" if room > 1 else ""


async def tick(app: Application, busy: Callable[[], bool]) -> None:
    """Repaint the bar: every 0.1s while `busy()`, else once a second for the clock."""
    while True:
        app.invalidate()
        await asyncio.sleep(TICK_BUSY_S if busy() else TICK_IDLE_S)


class _AnsiLexer(Lexer):
    """Render the C.* escapes the rest of js already prints."""

    def lex_document(self, document: Document):
        lines = document.lines

        def get_line(lineno: int):
            return to_formatted_text(ANSI(lines[lineno]))

        return get_line


class Scrollback:
    """A read-only buffer that follows its own tail. `append` is loop-thread only;
    `_ScreenStdout` is what gets it there from anywhere."""

    def __init__(self) -> None:
        self.buffer = Buffer(read_only=True, document=Document("", 0))
        self._pending = ""
        # Replaceable regions (reasoning blocks, the live answer block), in
        # buffer order; each keeps its offsets current as text around it moves.
        self._spans: list[ReasoningBlock | AnswerSpan] = []
        self._answer: AnswerSpan | None = None
        # The reasoning_toggle key a collapsed block names; "" names none.
        self.toggle_key = keys.describe(keys.ACTION_BY_NAME["reasoning_toggle"].keys[0])

    def append(self, text: str) -> None:
        self._pending += _drawable(text)
        if "\n" not in self._pending and len(self._pending) < 200:
            return
        self._commit()

    def _commit(self) -> None:
        if not self._pending:
            return
        doc = self.buffer.document
        text = doc.text + self._pending
        self._pending = ""
        self._set_text(text, doc.cursor_position, follow=doc.is_cursor_at_the_end)

    def _unfolded_rows(self) -> int:
        """The rows unfolded blocks, tool exchanges and reasoning alike, show
        beyond their folded form (`unfolded_rows`): an unfolded block never
        pushes earlier text, or its own top, out of the scrollback, and
        folding it back gives the rows back."""
        return sum(block.unfolded_rows() for block in self._spans)

    def _set_text(self, text: str, cursor: int, *, follow: bool) -> None:
        lines = text.split("\n")
        trimmed = "\n".join(lines[-(int(fset.setting_value("SCROLLBACK_LINES_MAX")) + self._unfolded_rows()):])
        removed = len(text) - len(trimmed)
        if removed:
            for block in self._spans:
                block.start = max(0, block.start - removed)
                block.end = max(0, block.end - removed)
            self._spans[:] = [block for block in self._spans if block.end > 0]
        self.buffer.set_document(
            Document(trimmed, len(trimmed) if follow else max(0, min(cursor - removed, len(trimmed)))),
            bypass_readonly=True,
        )

    def flush(self) -> None:
        self._commit()

    def reasoning(self, level: int) -> ReasoningBlock:
        self.flush()
        block = ReasoningBlock(self, level, len(self.buffer.text))
        self._spans.append(block)
        return block

    def answer_update(self, rendered: str) -> None:
        """Show `rendered` as the live answer block, replacing the previous one."""
        if self._answer is None:
            self.flush()
            self._answer = AnswerSpan(len(self.buffer.text))
            self._spans.append(self._answer)
        self._answer.text = rendered
        self._render_span(self._answer, new_text=True)

    def answer_commit(self, rendered: str) -> None:
        """Replace the live answer block with its final text and close it."""
        self.answer_update(rendered)
        if self._answer in self._spans:
            self._spans.remove(self._answer)
        self._answer = None

    def exchange_open(self, rendered: str) -> AnswerSpan:
        """Show `rendered` at the tail as a block `exchange_close` can replace
        after later text has been appended."""
        self.flush()
        span = AnswerSpan(len(self.buffer.text), self)
        span.text = rendered
        self._render_span(span, new_text=True)
        return span

    def exchange_update(self, span: AnswerSpan, rendered: str, expanded: str | None = None) -> None:
        """Redraw an open exchange where it stands; it stays open. With
        ``expanded``, the whole form of what it holds so far, the
        reasoning_toggle key unfolds it already."""
        span.text = rendered
        if expanded is not None:
            span.expanded = expanded
        self._render_span(span)

    def exchange_close(self, span: AnswerSpan, rendered: str, expanded: str | None = None) -> None:
        """Replace an open exchange with its final text, where it stands.
        With ``expanded``, its whole form, the exchange stays folded with an
        expand hint, and the reasoning_toggle key unfolds and folds it."""
        span.text = rendered
        span.expanded = expanded
        self._render_span(span)
        if span in self._spans and expanded is None:
            self._spans.remove(span)

    def _render_span(self, block: ReasoningBlock | AnswerSpan, *, new_text: bool = False) -> None:
        self.flush()
        doc = self.buffer.document
        if block not in self._spans:
            if not new_text:
                return
            block.start = block.end = len(doc.text)
            self._spans.append(block)
        rendered = _drawable(block.render())
        end = block.end
        change = len(rendered) - (end - block.start)
        text = doc.text[:block.start] + rendered + doc.text[end:]
        for following in self._spans:
            if following is not block and following.start >= end:
                following.start += change
                following.end += change
        block.end = block.start + len(rendered)
        cursor = doc.cursor_position
        if cursor >= end:
            cursor += change
        elif cursor >= block.start:
            cursor = min(cursor, block.end)
        self._set_text(text, cursor, follow=doc.is_cursor_at_the_end)

    def toggle_reasoning(self) -> bool:
        """Unfold every reasoning block and folded tool exchange, or fold them
        all back when none is folded."""
        blocks = [
            block for block in self._spans
            if (isinstance(block, ReasoningBlock) and block.level and block.text)
            or (isinstance(block, AnswerSpan) and block.expanded is not None)
        ]
        collapse = not any(block.collapsed for block in blocks)
        for block in blocks:
            block.manual = True
            block.collapsed = collapse
            self._render_span(block)
        return bool(blocks)


class AnswerSpan:
    """The live answer block, or a tool exchange: rendered text that is
    replaced until committed. A folded exchange keeps its whole form in
    `expanded` and shows `text` with an expand hint while `collapsed`."""

    def __init__(self, position: int, owner: Scrollback | None = None) -> None:
        self.start = self.end = position
        self.text = ""
        self.expanded: str | None = None
        self.collapsed = True
        self.manual = False
        self.owner = owner

    def unfolded_rows(self) -> int:
        """The rows it shows beyond its folded form while unfolded."""
        if self.expanded is None or self.collapsed:
            return 0
        return max(0, self.expanded.count("\n") - self.text.count("\n"))

    def render(self) -> str:
        if self.expanded is None or not self.text.strip():
            return self.text
        if not self.collapsed:
            return self.expanded
        key = self.owner.toggle_key if self.owner is not None else ""
        if not key:
            return self.text
        first, sep, rest = self.text.partition("\n")
        hint = msgs.TOOL_EXPAND.body_runs(key=key)
        width = display.block_width()
        if display.cells(first) + 2 + display.cells(fset.to_plain(hint)) <= width and not _goes_on(first, rest, width):
            return f"{first}  {display.drawn_rows(hint, width, 'COLOR_DIM')[0]}{sep}{rest}"
        # On rows of its own after the block, wrapped like any row, so a
        # header that wraps stays in one piece.
        rows = display.drawn_rows(hint, max(width - 2, 1), "COLOR_DIM")
        body = self.text.rstrip("\n")
        return body + "".join(f"\n  {row}" for row in rows) + self.text[len(body):]


def _goes_on(first: str, rest: str, width: int) -> bool:
    """Whether the row after ``first`` may be ``first`` word-wrapped onto it:
    its first word would not have fit on ``first``, so a wrap could have put
    it there."""
    following = fset.plain(rest.partition("\n")[0]).removeprefix(display.RAIL).split()
    if not following:
        return False
    return display.cells(first.rstrip(" ")) + 1 + display.cells(following[0]) > width


class ReasoningBlock:
    """A replaceable scrollback region retaining the provider's original text."""

    def __init__(self, owner: Scrollback, level: int, position: int) -> None:
        self.owner = owner
        self.level = level
        self.start = self.end = position
        self.text = ""
        self.collapsed = False
        self.manual = False
        self.tokens: int | None = None
        self._cleaner = display.StreamCleaner()
        # The finished lines of `text` wrapped, kept between renders: a chunk
        # rewraps only the line still open. (width, length of text wrapped, rows)
        self._wrapped: tuple[int, int, str] = (0, 0, "")
        # The rows of the open line that more text cannot change, tabs
        # expanded: (columns of the line they hold, rows)
        self._open: tuple[int, str] = (0, "")

    def _body(self) -> str:
        """`text` word-wrapped to the window, reusing the rows wrapped before:
        a chunk rewraps only the last row of the open line."""
        width = display.block_width()
        done_width, done, rows = self._wrapped
        if done_width != width:
            done, rows = 0, ""
            self._open = (0, "")
        last = self.text.rfind("\n") + 1
        if last > done:
            rows += display.wrap_words(self.text[done:last - 1], width) + "\n"
            done = last
            self._open = (0, "")
        self._wrapped = (width, done, rows)
        if done == len(self.text):
            return rows
        line = self.text[done:].expandtabs(display.TAB)
        held, open_rows = self._open
        settled, start = settled_rows(line, held, width, whole=False)
        open_rows += "".join(row + "\n" for row in settled)
        self._open = (start, open_rows)
        return rows + open_rows + (line[start:].lstrip(" ") if start else line)

    def unfolded_rows(self) -> int:
        """The rows it shows beyond its folded heading while open, at the
        level that folds it."""
        if self.level != 1 or self.collapsed:
            return 0
        return max(0, self.render().count("\n") - 1)

    def render(self) -> str:
        """Collapsed: `N tok reasoning  KEY to expand`, the count the provider
        gave or none. Open: the same heading, then the text wrapped at word
        boundaries to the window."""
        if self.level == 0:
            return ""
        count = msgs.REASONING_COUNT.body_runs(tokens=f"{self.tokens:,}") if self.tokens is not None else []
        if self.collapsed:
            hint = msgs.REASONING_EXPAND.body_runs(key=self.owner.toggle_key) if self.owner.toggle_key else []
            return self._heading(count, hint)
        body = self._body()
        heading = self._heading(count if self.level == 3 else [], [])
        return heading + grey(body + ("" if body.endswith("\n") else "\n"))

    @staticmethod
    def _heading(count: list[fset.Run], hint: list[fset.Run]) -> str:
        """`N tok reasoning` and the hint after it, in COLOR_REASONING,
        wrapped to the window."""
        parts = [*count, *([("", " ")] if count else []), *msgs.REASONING_HEADING.body_runs(), *hint]
        return "\n".join(display.drawn_rows(parts, display.block_width(), "COLOR_REASONING")) + "\n"

    def append(self, text: str) -> None:
        # Model text is cleaned here, before anything measures or wraps it.
        self.text += self._cleaner.feed(text)
        self.owner._render_span(self, new_text=True)

    def answer_started(self) -> None:
        if self.level == 1 and not self.manual and not self.collapsed:
            self.collapsed = True
            self.owner._render_span(self)

    def finish(self, tokens: int | None = None) -> None:
        self.tokens = tokens
        self.text += self._cleaner.flush()  # the text of a string sequence never ended
        self.answer_started()
        self.owner._render_span(self)


class ScreenReasoningDisplay:
    """Schedule reasoning and ordinary stdout writes on the same loop queue."""

    def __init__(self, loop, scrollback: Scrollback, app: Application, level: int) -> None:
        self._loop, self._scrollback, self._app = loop, scrollback, app
        self._level = level
        self._block: ReasoningBlock | None = None

    def _apply(self, method: str, *args) -> None:
        if self._block is None:
            self._block = self._scrollback.reasoning(self._level)
        getattr(self._block, method)(*args)
        self._app.invalidate()

    def append(self, text: str) -> None:
        self._loop.call_soon_threadsafe(self._apply, "append", text)

    def answer_started(self) -> None:
        self._loop.call_soon_threadsafe(self._apply, "answer_started")

    def finish(self, tokens: int | None = None) -> None:
        self._loop.call_soon_threadsafe(self._apply, "finish", tokens)


class ScreenLive:
    """`display.LiveSurface` over the scrollback. Calls are queued on the loop
    in the same order as ordinary stdout writes."""

    def __init__(self, loop, scrollback: Scrollback, app: Application) -> None:
        self._loop, self._scrollback, self._app = loop, scrollback, app

    def width(self) -> int:
        try:
            return max(20, self._app.output.get_size().columns - 1)
        except Exception:  # noqa: BLE001 - output not attached yet
            return 79

    def _apply(self, method: str, rendered: str) -> None:
        getattr(self._scrollback, method)(rendered)
        self._app.invalidate()

    def update(self, rendered: str) -> None:
        self._loop.call_soon_threadsafe(self._apply, "answer_update", rendered)

    def commit(self, rendered: str) -> None:
        self._loop.call_soon_threadsafe(self._apply, "answer_commit", rendered)


class ScreenExchange:
    """One tool exchange on the scrollback: drawn when the call starts,
    redrawn in place while it runs, and replaced by the finished exchange when
    it ends. Calls are queued on the loop in the same order as ordinary stdout
    writes."""

    def __init__(self, loop, scrollback: Scrollback, app: Application) -> None:
        self._loop, self._scrollback, self._app = loop, scrollback, app
        self._span: AnswerSpan | None = None

    def _open(self, rendered: str) -> None:
        self._span = self._scrollback.exchange_open(rendered)
        self._app.invalidate()

    def _close(self, rendered: str, expanded: str | None) -> None:
        if self._span is None:
            self._span = self._scrollback.exchange_open(rendered)
        self._scrollback.exchange_close(self._span, rendered, expanded)
        self._app.invalidate()

    def _update(self, rendered: str, expanded: str | None) -> None:
        if self._span is not None:
            self._scrollback.exchange_update(self._span, rendered, expanded)
            self._app.invalidate()

    def open(self, rendered: str) -> None:
        self._loop.call_soon_threadsafe(self._open, rendered)

    def update(self, rendered: str, expanded: str | None = None) -> None:
        """Redraw the running exchange; ``expanded`` is the whole form of the
        calls in it that have returned."""
        self._loop.call_soon_threadsafe(self._update, rendered, expanded)

    def close(self, rendered: str, expanded: str | None = None) -> None:
        """Finish the exchange; ``expanded`` is its whole form when ``rendered``
        is folded."""
        self._loop.call_soon_threadsafe(self._close, rendered, expanded)


class _ScreenStdout:
    """`sys.stdout` for the life of the app. Writes from executor threads are
    marshalled onto the loop; the app repaints on its own schedule."""

    def __init__(self, loop: asyncio.AbstractEventLoop, scrollback: Scrollback,
                 app: Application, real) -> None:
        self._loop = loop
        self._scrollback = scrollback
        self._app = app
        self._real = real
        self.encoding = getattr(real, "encoding", "utf-8")

    def write(self, s: str) -> int:
        self._loop.call_soon_threadsafe(self._append, s)
        return len(s)

    def _append(self, s: str) -> None:
        self._scrollback.append(s)
        self._app.invalidate()

    def flush(self) -> None:
        self._loop.call_soon_threadsafe(self._flush)

    def _flush(self) -> None:
        self._scrollback.flush()
        self._app.invalidate()

    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        return self._real.fileno()

    def write_folded(self, folded: str, whole: str) -> None:
        """A block shown as ``folded`` that the reasoning_toggle key unfolds
        to ``whole``, as a folded tool row is."""
        self._loop.call_soon_threadsafe(self._fold, folded, whole)

    def _fold(self, folded: str, whole: str) -> None:
        span = self._scrollback.exchange_open(folded)
        self._scrollback.exchange_close(span, folded, whole)
        self._app.invalidate()


class InputEditor:
    """The input line as a buffer the `:` ex line acts on."""

    def __init__(self, buffer: Buffer, submit: Callable[[], Coroutine]) -> None:
        self._buffer = buffer
        self.submit = submit

    @property
    def text(self) -> str:
        return self._buffer.text

    @text.setter
    def text(self, value: str) -> None:
        self._buffer.set_document(Document(value, len(value)), bypass_readonly=True)

    def insert(self, text: str) -> None:
        self._buffer.insert_text(text)

    async def run(self, argv: list[str]) -> int:
        """Run a program on the real terminal, the screen suspended until it
        exits; its exit status."""
        return await run_in_terminal(lambda: subprocess.call(argv), in_executor=True)


@dataclass(frozen=True)
class Handler:
    """What an action runs, and where: prompt_toolkit's handler, filter and eager flag."""

    fn: Callable
    filter: FilterOrBool = True
    eager: bool = False


def bind_actions(kb: KeyBindings, keymap: keys.Keymap, handlers: dict[str, Handler]) -> None:
    """Add each key sequence ``keymap`` gives an action to ``kb``, running that
    action's handler. Every action in ``keymap`` has one in ``handlers``."""
    for name, sequences in keymap.items():
        handler = handlers[name]
        for sequence in sequences:
            kb.add(*sequence, filter=handler.filter, eager=handler.eager)(handler.fn)


def build_app(
    *,
    prompt: str | Callable[[], str],
    history,
    completer,
    on_line: Callable[[str], Coroutine],
    on_interrupt: Callable[[], bool | None],
    on_eof: Callable[[], Awaitable | None],
    status: Callable[[int], str] = lambda width: "",
    status_colours: Callable[[], str] = status_style,
    editing_mode: Callable[[], str] = lambda: "emacs",
    on_ex: Callable[[str, InputEditor], Coroutine] | None = None,
    key_bindings: KeyBindingsBase | None = None,
    keymap: keys.Keymap | None = None,
    echo: Callable[[str], bool] = lambda line: True,
    user_line: Callable[[str], str] | None = None,
    layout_factory: Callable[[Layout], Layout] | None = None,
    application_factory: Callable | None = None,
    complete_while_typing: bool = False,
    complete_tab: Callable | None = None,
    handler_factory: Callable[[dict[str, Handler]], dict[str, Handler]] | None = None,
) -> tuple[Application, Scrollback]:
    """`status(width)` renders the bar; `status_colours()` is its style, read on
    every repaint so a changed setting shows on the next invalidate. In vi mode
    the input is a multi-line buffer: Enter is a newline and `:` in normal mode
    opens the ex line, whose text goes to ``on_ex``. ``keymap`` names the keys
    of each action (`js.keys`); None is the defaults. The history_search key
    opens a reverse incremental search over ``history``. ``key_bindings`` are
    added after the screen's own and win a shared key. ``on_eof`` quits: it
    ends the app with `app.exit()`, and an awaitable it returns runs as the
    key's handler. The interrupt key calls ``on_interrupt``; when that returns
    false (no turn to cancel), the key clears an input line with text and
    arms the exit, and on an empty line a press within `EXIT_ARM_TIMEOUT` of
    the last one quits, else arms. The eof key quits at an empty line,
    deletes the character under the cursor inside a line, and at the end of
    a line with text arms the exit, quitting on a second press within the
    same window. While an exit is armed a row under the input says so. A
    sent line's paste markers (`js.pastes`) are expanded before ``on_line`` gets it; the
    scrollback echoes the line as typed when ``echo`` of the expanded line is true."""
    scrollback = Scrollback()
    input_buffer = Buffer(
        name=DEFAULT_BUFFER,  # the buffer js.pastes collapses large pastes in
        history=history,
        completer=completer,
        complete_while_typing=complete_while_typing,
        enable_history_search=True,
        multiline=vi_mode,
    )
    ex_buffer = Buffer(multiline=False)
    search_toolbar = SearchToolbar(ignore_case=True)
    kb = KeyBindings()

    async def submit() -> None:
        line = input_buffer.text
        input_buffer.append_to_history()
        input_buffer.reset()
        sent = pastes.expand(line).strip()
        if sent and echo(sent):
            scrollback.append((display.user_line if user_line is None else user_line)(line))
        await on_line(sent)

    editor = InputEditor(input_buffer, submit)

    async def _enter(event) -> None:
        await submit()

    def _ex_open(event) -> None:
        event.app.layout.focus(ex_buffer)
        event.app.vi_state.input_mode = InputMode.INSERT

    def _ex_close(app) -> str:
        text = ex_buffer.text
        ex_buffer.reset()
        app.layout.focus(input_buffer)
        app.vi_state.input_mode = InputMode.NAVIGATION
        return text

    async def _ex_run(event) -> None:
        text = _ex_close(event.app).strip()
        if text and on_ex is not None:
            await on_ex(text, editor)

    def _ex_cancel(event) -> None:
        _ex_close(event.app)

    # The action whose next press quits, and the loop time its window closes;
    # empty when no exit is armed. While it is set, the hint row shows.
    armed: dict = {}

    def _expire(until: float) -> None:
        if armed.get("until") == until:
            armed.clear()
            get_app().invalidate()

    def _arm(action: str) -> None:
        loop = asyncio.get_running_loop()
        until = loop.time() + float(fset.setting_value("EXIT_ARM_TIMEOUT"))
        armed.update(action=action, until=until)
        loop.call_at(until, _expire, until)

    def _arm_or_quit(action: str):
        armed_now = armed.get("action") == action and asyncio.get_running_loop().time() < armed["until"]
        if armed_now:
            armed.clear()
            return on_eof()
        _arm(action)
        return None

    def _ctrl_c(event):
        if on_interrupt():
            armed.clear()
            return None
        if event.current_buffer is ex_buffer:
            armed.clear()
            _ex_close(event.app)
            return None
        if input_buffer.text:
            input_buffer.reset()
            _arm("interrupt")
            return None
        return _arm_or_quit("interrupt")

    def _ctrl_d(event):
        if event.current_buffer is ex_buffer:
            ex_buffer.delete()
            return None
        if not input_buffer.text:
            return on_eof()
        if input_buffer.document.is_cursor_at_the_end:
            return _arm_or_quit("eof")
        input_buffer.delete()
        return None

    def _exit_hint():
        sequences = keymap.get(armed.get("action", ""), ())
        key = keys.describe(sequences[0]) if sequences else ""
        return ANSI(msgs.EXIT_ARMED.body(key=key))

    def _toggle_reasoning(event) -> None:
        if scrollback.toggle_reasoning():
            event.app.invalidate()

    def _tab(event) -> None:
        if complete_tab is not None:
            complete_tab(event)
            return
        # The first Tab fills in what every match shares and lists them all;
        # each Tab after that selects the next one.
        b = event.current_buffer
        if b.complete_state:
            b.complete_next()
        else:
            b.start_completion(insert_common_part=True)

    def _page() -> int:
        return max(1, get_app().output.get_size().rows - 3)

    keymap = keymap if keymap is not None else keys.default_keymap()
    handlers = {
        "submit": Handler(_enter, has_focus(input_buffer) & ~vi_mode),
        "history_search": Handler(keys.history_search, has_focus(input_buffer) | is_searching),
        "ex_open": Handler(_ex_open, has_focus(input_buffer) & vi_navigation_mode),
        "ex_run": Handler(_ex_run, has_focus(ex_buffer)),
        "ex_cancel": Handler(_ex_cancel, has_focus(ex_buffer), eager=True),
        "interrupt": Handler(_ctrl_c),
        "eof": Handler(_ctrl_d),
        "suspend": Handler(lambda event: event.app.suspend_to_background()),
        "reasoning_toggle": Handler(_toggle_reasoning),
        "redraw": Handler(lambda event: event.app.renderer.clear()),
        "scroll_up": Handler(lambda event: scrollback.buffer.cursor_up(count=_page())),
        "scroll_down": Handler(lambda event: scrollback.buffer.cursor_down(count=_page())),
        "complete": Handler(_tab),
    }
    bind_actions(kb, keymap, handlers if handler_factory is None else handler_factory(handlers))
    toggle = keymap.get("reasoning_toggle", ())
    scrollback.toggle_key = keys.describe(toggle[0]) if toggle else ""

    def _status_text():
        try:
            return ANSI(accent_clock(status(get_app().output.get_size().columns)))
        except Exception:  # noqa: BLE001 — the bar must never take the screen down
            return " "

    def _style() -> Style:
        try:
            colours = status_colours()
        except Exception:  # noqa: BLE001
            colours = ""
        return _status_style_sheet(colours)

    def _input_height() -> Dimension:
        try:
            rows = get_app().output.get_size().rows
        except Exception:  # noqa: BLE001 - output not attached yet
            rows = 24
        return Dimension(min=1, max=max(10, rows // 2))

    def _prompt() -> str:
        return prompt() if callable(prompt) else prompt

    layout = Layout(
        FloatContainer(
            HSplit([
                Window(BufferControl(buffer=scrollback.buffer, lexer=_AnsiLexer(), focusable=False),
                       wrap_lines=True),
                Window(FormattedTextControl(_status_text), height=1, style="class:status"),
                Window(BufferControl(buffer=input_buffer,
                                     input_processors=[HighlightIncrementalSearchProcessor()],
                                     search_buffer_control=search_toolbar.control,
                                     lexer=None),
                       # A long line wraps; the box grows to half the screen before it scrolls.
                       height=_input_height, dont_extend_height=True, wrap_lines=True,
                       get_line_prefix=lambda lineno, wrap: to_formatted_text(ANSI(_prompt() if lineno == 0 and not wrap
                                                                                   else " " * display.cells(_prompt())))),
                ConditionalContainer(
                    Window(FormattedTextControl(_exit_hint), height=1),
                    filter=Condition(lambda: bool(armed)),
                ),
                ConditionalContainer(
                    Window(BufferControl(buffer=ex_buffer), height=1, get_line_prefix=lambda *_: ":"),
                    filter=has_focus(ex_buffer),
                ),
                search_toolbar,
            ], style=screen_style),
            floats=[Float(xcursor=True, ycursor=True, transparent=True,
                          content=MultiColumnCompletionsMenu())],
        ),
        focused_element=input_buffer,
    )
    def _editing_mode() -> EditingMode:
        return EditingMode.VI if editing_mode() == "vi" else EditingMode.EMACS

    def _sync_editing_mode(app: Application) -> None:
        app.editing_mode = _editing_mode()

    if layout_factory is not None:
        layout = layout_factory(layout)
    factory = Application if application_factory is None else application_factory
    app = factory(
        layout=layout,
        key_bindings=kb if key_bindings is None else merge_key_bindings([kb, key_bindings]),
        style=DynamicStyle(_style),
        # Truecolor always: on TERM=linux prompt_toolkit would otherwise pick
        # 4-bit and snap the bar's hex to the nearest of sixteen colours.
        color_depth=ColorDepth.DEPTH_24_BIT,
        editing_mode=_editing_mode(),
        before_render=_sync_editing_mode,
        full_screen=True,
        mouse_support=False,
    )
    return app, scrollback


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    return _ANSI_ESCAPE.sub("", text)


class capture_stdio:
    """Route sys.stdout/sys.stderr into the scrollback while the app runs."""

    def __init__(self, loop, scrollback: Scrollback, app: Application) -> None:
        self._proxy = _ScreenStdout(loop, scrollback, app, sys.__stdout__)

    def __enter__(self):
        self._saved = (sys.stdout, sys.stderr)
        sys.stdout = sys.stderr = self._proxy
        return self

    def __exit__(self, *exc) -> None:
        sys.stdout, sys.stderr = self._saved
