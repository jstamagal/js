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
from prompt_toolkit.filters import FilterOrBool, has_focus, is_searching, vi_mode, vi_navigation_mode
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
from .context_budget import estimate_text_tokens
from .reasoning_display import grey

SCROLLBACK_LINES = 5000
THROBBER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
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
    return f"{code}{line[:end]}\x1b[39m{line[end:]}" if code else line


@functools.lru_cache(maxsize=8)
def _status_style_sheet(colours: str) -> Style:
    return Style.from_dict({"status": colours})


def format_count(n: int) -> str:
    """Output tokens as a heartbeat: hundreds under 10k, then one-decimal k."""
    if n < 10_000:
        return f"{n // 100 * 100:,}"
    return f"{n / 1000:.1f}k"


def throbber_frame(now: float) -> str:
    return THROBBER[int(now / TICK_BUSY_S) % len(THROBBER)]


def turn_centre(status, *, now: float, show_bytes: bool) -> tuple[str, int | None]:
    """(phase, output_tokens) for the bar's centre group from a TurnStatus.

    A running tool wins, then compaction, then response bytes before the first
    token (when the net channel shows them), then the output-token count.
    """
    if status.tool:
        extra = f" +{status.tool_extra}" if status.tool_extra else ""
        elapsed = int(max(0.0, now - status.tool_started))
        return f"{status.tool}{extra} {elapsed}s", None
    if status.compacting:
        return _field(msgs.STATUS_COMPACTING), None
    if show_bytes and status.net_bytes:
        return f"{status.net_bytes // 100 * 100:,}B", None
    return "", status.output_tokens or None


def _field(message: msgs.Message, **fields) -> str:
    """One status bar field through its /fset format, palette colours kept."""
    return message.line(**fields)


def _width(text: str) -> int:
    """Columns ``text`` takes on the bar, colour escapes not counted."""
    return len(fset.plain(text))


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
    cost`, where `cost` is the session's spend (`usage.status_text`). Each
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
        room = max(_width(clock_text), width - len(tail) - 1)
        line = " ".join(filter(None, (left[:room], tail)))
    if _width(line) > width:
        line = fset.plain(line)[:width]
    return line + " " * (width - _width(line))


def session_short(session_file) -> str:
    """The session as the bar names it: the first 8 hex chars of a generated
    `<timestamp>-<hex>` stem, else the whole stem, the name it was given."""
    stem = getattr(session_file, "stem", "") or ""
    return stem.rsplit("-", 1)[-1][:8] if session_store.is_generated(stem) else stem


def _elide(text: str, room: int) -> str:
    """`text` in at most `room` cells, cut short with an ellipsis."""
    if len(text) <= room:
        return text
    return text[:room - 1] + "…" if room > 1 else ""


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

    def _set_text(self, text: str, cursor: int, *, follow: bool) -> None:
        lines = text.split("\n")
        trimmed = "\n".join(lines[-SCROLLBACK_LINES:])
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
        span = AnswerSpan(len(self.buffer.text))
        span.text = rendered
        self._render_span(span, new_text=True)
        return span

    def exchange_update(self, span: AnswerSpan, rendered: str) -> None:
        """Redraw an open exchange where it stands; it stays open."""
        span.text = rendered
        self._render_span(span)

    def exchange_close(self, span: AnswerSpan, rendered: str) -> None:
        """Replace an open exchange with its final text, where it stands."""
        span.text = rendered
        self._render_span(span)
        if span in self._spans:
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
        blocks = [
            block for block in self._spans
            if isinstance(block, ReasoningBlock) and block.level and block.text
        ]
        collapse = not any(block.collapsed for block in blocks)
        for block in blocks:
            block.manual = True
            block.collapsed = collapse
            self._render_span(block)
        return bool(blocks)


class AnswerSpan:
    """The live answer block: rendered Markdown that is replaced until committed."""

    def __init__(self, position: int) -> None:
        self.start = self.end = position
        self.text = ""

    def render(self) -> str:
        return self.text


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

    def render(self) -> str:
        if self.level == 0:
            return ""
        heading = "── reasoning ──"
        if self.collapsed or self.level == 3:
            count = str(self.tokens) if self.tokens is not None else f"~{estimate_text_tokens(self.text)}"
            heading += f" {count} tok"
        if self.collapsed:
            hint = f"  {self.owner.toggle_key} to expand" if self.owner.toggle_key else ""
            return grey(heading + hint + "\n")
        return grey(heading + "\n" + self.text + ("" if self.text.endswith("\n") else "\n"))

    def append(self, text: str) -> None:
        self.text += text
        self.owner._render_span(self, new_text=True)

    def answer_started(self) -> None:
        if self.level == 1 and not self.manual and not self.collapsed:
            self.collapsed = True
            self.owner._render_span(self)

    def finish(self, tokens: int | None = None) -> None:
        self.tokens = tokens
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

    def _close(self, rendered: str) -> None:
        if self._span is None:
            self._scrollback.append(rendered)
        else:
            self._scrollback.exchange_close(self._span, rendered)
        self._app.invalidate()

    def _update(self, rendered: str) -> None:
        if self._span is not None:
            self._scrollback.exchange_update(self._span, rendered)
            self._app.invalidate()

    def open(self, rendered: str) -> None:
        self._loop.call_soon_threadsafe(self._open, rendered)

    def update(self, rendered: str) -> None:
        self._loop.call_soon_threadsafe(self._update, rendered)

    def close(self, rendered: str) -> None:
        self._loop.call_soon_threadsafe(self._close, rendered)


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
    on_interrupt: Callable[[], None],
    on_eof: Callable[[], Awaitable | None],
    status: Callable[[int], str] = lambda width: "",
    status_colours: Callable[[], str] = status_style,
    editing_mode: Callable[[], str] = lambda: "emacs",
    on_ex: Callable[[str, InputEditor], Coroutine] | None = None,
    key_bindings: KeyBindingsBase | None = None,
    keymap: keys.Keymap | None = None,
    echo: Callable[[str], bool] = lambda line: True,
) -> tuple[Application, Scrollback]:
    """`status(width)` renders the bar; `status_colours()` is its style, read on
    every repaint so a changed setting shows on the next invalidate. In vi mode
    the input is a multi-line buffer: Enter is a newline and `:` in normal mode
    opens the ex line, whose text goes to ``on_ex``. ``keymap`` names the keys
    of each action (`js.keys`); None is the defaults. The history_search key
    opens a reverse incremental search over ``history``. ``key_bindings`` are
    added after the screen's own and win a shared key. ``on_eof`` runs on the
    eof key at an empty input line and ends the app with `app.exit()`; an
    awaitable it returns runs as the key's handler. A sent line's paste
    markers (`js.pastes`) are expanded before ``on_line`` gets it; the
    scrollback echoes the line as typed when ``echo`` of the expanded line is true."""
    scrollback = Scrollback()
    input_buffer = Buffer(
        name=DEFAULT_BUFFER,  # the buffer js.pastes collapses large pastes in
        history=history,
        completer=completer,
        complete_while_typing=False,
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
            scrollback.append(display.user_line(line))
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

    def _ctrl_d(event):
        if input_buffer.text:
            input_buffer.delete()
            return None
        return on_eof()

    def _toggle_reasoning(event) -> None:
        if scrollback.toggle_reasoning():
            event.app.invalidate()

    def _tab(event) -> None:
        # The first Tab fills in what every match shares and lists them all;
        # each Tab after that selects the next one.
        b = input_buffer
        if b.complete_state:
            b.complete_next()
        else:
            b.start_completion(insert_common_part=True)

    def _page() -> int:
        return max(1, get_app().output.get_size().rows - 3)

    keymap = keymap if keymap is not None else keys.default_keymap()
    bind_actions(kb, keymap, {
        "submit": Handler(_enter, has_focus(input_buffer) & ~vi_mode),
        "history_search": Handler(keys.history_search, has_focus(input_buffer) | is_searching),
        "ex_open": Handler(_ex_open, has_focus(input_buffer) & vi_navigation_mode),
        "ex_run": Handler(_ex_run, has_focus(ex_buffer)),
        "ex_cancel": Handler(_ex_cancel, has_focus(ex_buffer), eager=True),
        "interrupt": Handler(lambda event: on_interrupt()),
        "eof": Handler(_ctrl_d),
        "suspend": Handler(lambda event: event.app.suspend_to_background()),
        "reasoning_toggle": Handler(_toggle_reasoning),
        "redraw": Handler(lambda event: event.app.renderer.clear()),
        "scroll_up": Handler(lambda event: scrollback.buffer.cursor_up(count=_page())),
        "scroll_down": Handler(lambda event: scrollback.buffer.cursor_down(count=_page())),
        "complete": Handler(_tab),
    })
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
                       height=Dimension(min=1, max=10), dont_extend_height=True,
                       get_line_prefix=lambda lineno, wrap: to_formatted_text(ANSI(_prompt() if lineno == 0 and not wrap
                                                                                   else " " * len(_plain(_prompt()))))),
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

    app = Application(
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
