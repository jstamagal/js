"""Three-region screen for the async REPL: scrollback, status bar, input line.

Output never touches the input line because it is a different window. Every
`sys.stdout`/`sys.stderr` write during the REPL is marshalled onto the loop and
appended to the scrollback buffer; the input buffer is untouched.
"""
from __future__ import annotations

import asyncio
import contextlib
import functools
import subprocess
import sys
from collections.abc import Awaitable, Callable, Coroutine
from contextvars import ContextVar
from dataclasses import dataclass

from prompt_toolkit.application import Application, get_app, run_in_terminal
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.enums import DEFAULT_BUFFER, EditingMode
from prompt_toolkit.filters import (
    Condition,
    FilterOrBool,
    has_focus,
    is_searching,
    vi_insert_mode,
    vi_mode,
    vi_navigation_mode,
)
from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from prompt_toolkit.key_binding import KeyBindings, KeyBindingsBase, merge_key_bindings
from prompt_toolkit.key_binding.vi_state import InputMode
from prompt_toolkit.layout import (
    ConditionalContainer,
    DynamicContainer,
    Float,
    FloatContainer,
    HSplit,
    Layout,
    Window,
)
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.menus import MultiColumnCompletionsMenu
from prompt_toolkit.layout.processors import HighlightIncrementalSearchProcessor
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.styles import DynamicStyle, Style
from prompt_toolkit.widgets import SearchToolbar

from . import (
    display,
    fset,
    keys,
    messages as msgs,
    pastes,
    screen_scrollback,
    status_bar,
    text_layout,
)

# --- status bar ------------------------------------------------------------

# The scrollback that ``sys.stdout`` writes made in this context land in; None
# is the screen's own (window 1). A subagent's window sets it for that
# subagent's task, so what it prints lands in its window.
writing_to: ContextVar[screen_scrollback.Scrollback | None] = ContextVar("js_screen_writing_to", default=None)


@contextlib.contextmanager
def writing(scrollback: screen_scrollback.Scrollback):
    """Send this context's ``sys.stdout`` writes to ``scrollback``."""
    token = writing_to.set(scrollback)
    try:
        yield
    finally:
        writing_to.reset(token)


class View:
    """One window: its number, its scrollback, whether it has output not yet
    seen, and what it shows (``owner``: None for window 1, else the object
    that opened it)."""

    def __init__(self, number: int, scrollback: screen_scrollback.Scrollback, owner=None) -> None:
        self.number = number
        self.scrollback = scrollback
        self.owner = owner
        self.active = False
        self.window: Window | None = None


class Windows:
    """ircII-style windows over the scrollback region: window 1 is the main
    conversation; ``open`` numbers the next one from 2 to `LAST`. One window
    shows at a time; output to a hidden window marks it active until it is
    shown."""

    LAST = 9

    def __init__(self) -> None:
        self._views: dict[int, View] = {}
        self.current = 1
        self.invalidate: Callable[[], None] = lambda: None

    def set_main(self, scrollback: screen_scrollback.Scrollback) -> None:
        """Window 1 shows ``scrollback``."""
        if 1 not in self._views:
            self._views[1] = View(1, scrollback)

    def view(self, number: int | None = None) -> View | None:
        return self._views.get(self.current if number is None else number)

    def scrollback(self) -> screen_scrollback.Scrollback:
        """The scrollback of the window on screen."""
        return self._views[self.current].scrollback

    def numbers(self) -> list[int]:
        return sorted(self._views)

    def open(self, owner=None) -> View | None:
        """A new window numbered with the lowest free number from 2 to
        `LAST`, its output marking it active; None when every one is taken."""
        free = next((n for n in range(2, self.LAST + 1) if n not in self._views), None)
        if free is None:
            return None
        view = self._views[free] = View(free, screen_scrollback.Scrollback(), owner)
        view.scrollback.toggle_key = self._views[1].scrollback.toggle_key if 1 in self._views else ""
        view.scrollback.changed = functools.partial(self._changed, view)
        self.invalidate()
        return view

    def _changed(self, view: View) -> None:
        if view.number != self.current and not view.active and self._views.get(view.number) is view:
            view.active = True
            self.invalidate()

    def switch(self, number: int) -> bool:
        """Show window ``number`` and clear its activity mark; False when
        there is no such window."""
        view = self._views.get(number)
        if view is None:
            return False
        self.current = number
        view.active = False
        self.invalidate()
        return True

    def close(self, number: int) -> View | None:
        """Take window ``number`` off the screen; window 1 stays. The view
        closed, or None when there was none to close."""
        if number == 1:
            return None
        view = self._views.pop(number, None)
        if view is not None and self.current == number:
            self.current = 1
            self._views[1].active = False
        self.invalidate()
        return view

    def activity(self) -> list[int]:
        """The windows with output not yet seen, in number order."""
        return [n for n in self.numbers() if self._views[n].active and n != self.current]

    def container(self, make: Callable[[screen_scrollback.Scrollback], Window]) -> DynamicContainer:
        """The scrollback region: the window on screen, each view keeping its
        own prompt_toolkit Window (and so its own scroll position)."""
        def current() -> Window:
            view = self._views[self.current]
            if view.window is None:
                view.window = make(view.scrollback)
            return view.window
        return DynamicContainer(current)


class ScreenReasoningDisplay:
    """Schedule reasoning and ordinary stdout writes on the same loop queue."""

    def __init__(self, loop, scrollback: screen_scrollback.Scrollback, app: Application, level: int) -> None:
        self._loop, self._scrollback, self._app = loop, scrollback, app
        self._level = level
        self._block: screen_scrollback.ReasoningBlock | None = None

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

    def __init__(self, loop, scrollback: screen_scrollback.Scrollback, app: Application) -> None:
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

    def __init__(self, loop, scrollback: screen_scrollback.Scrollback, app: Application) -> None:
        self._loop, self._scrollback, self._app = loop, scrollback, app
        self._span: screen_scrollback.AnswerSpan | None = None

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

    def __init__(self, loop: asyncio.AbstractEventLoop, scrollback: screen_scrollback.Scrollback,
                 app: Application, real) -> None:
        self._loop = loop
        self._scrollback = scrollback
        self._app = app
        self._real = real
        self.encoding = getattr(real, "encoding", "utf-8")

    def _target(self) -> screen_scrollback.Scrollback:
        return writing_to.get() or self._scrollback

    def write(self, s: str) -> int:
        self._loop.call_soon_threadsafe(self._append, self._target(), s)
        return len(s)

    def _append(self, scrollback: screen_scrollback.Scrollback, s: str) -> None:
        scrollback.append(s)
        self._app.invalidate()

    def flush(self) -> None:
        self._loop.call_soon_threadsafe(self._flush, self._target())

    def _flush(self, scrollback: screen_scrollback.Scrollback) -> None:
        scrollback.flush()
        self._app.invalidate()

    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        return self._real.fileno()

    def write_folded(self, folded: str, whole: str) -> None:
        """A block shown as ``folded`` that the reasoning_toggle key unfolds
        to ``whole``, as a folded tool row is."""
        self._loop.call_soon_threadsafe(self._fold, self._target(), folded, whole)

    def _fold(self, scrollback: screen_scrollback.Scrollback, folded: str, whole: str) -> None:
        span = scrollback.exchange_open(folded)
        scrollback.exchange_close(span, folded, whole)
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
    turn_active: Callable[[], bool] = lambda: False,
    status: Callable[[int], str] = lambda width: "",
    status_colours: Callable[[], str] = status_bar.status_style,
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
    windows: Windows | None = None,
) -> tuple[Application, screen_scrollback.Scrollback]:
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
    scrollback echoes the line as typed when ``echo`` of the expanded line is true.
    ``windows`` holds the scrollback region's windows (`Windows`); the
    returned scrollback is window 1's. The window_N keys show window N, and
    the scroll, reasoning_toggle and echo act on the window on screen."""
    scrollback = screen_scrollback.Scrollback()
    windows = Windows() if windows is None else windows
    windows.set_main(scrollback)
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
            windows.scrollback().append((display.user_line if user_line is None else user_line)(line))
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

    def _cancel_turn(event):
        if turn_active() and on_interrupt():
            armed.clear()

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
        key = msgs.describe_keys(sequences[0]) if sequences else ""
        return ANSI(msgs.EXIT_ARMED.body(key=key))

    def _toggle_reasoning(event) -> None:
        if windows.scrollback().toggle_reasoning():
            event.app.invalidate()

    def _switch(number: int):
        def handler(event) -> None:
            if windows.switch(number):
                event.app.invalidate()
        return handler

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
        "cancel_turn": Handler(
            _cancel_turn,
            has_focus(input_buffer) & ~is_searching & ~vi_insert_mode
            & Condition(lambda: turn_active() and input_buffer.complete_state is None),
        ),
        "eof": Handler(_ctrl_d),
        "suspend": Handler(lambda event: event.app.suspend_to_background()),
        "reasoning_toggle": Handler(_toggle_reasoning),
        "redraw": Handler(lambda event: event.app.renderer.clear()),
        "scroll_up": Handler(lambda event: windows.scrollback().buffer.cursor_up(count=_page())),
        "scroll_down": Handler(lambda event: windows.scrollback().buffer.cursor_down(count=_page())),
        "complete": Handler(_tab),
        **{f"window_{n}": Handler(_switch(n)) for n in range(1, Windows.LAST + 1)},
    }
    bind_actions(kb, keymap, handlers if handler_factory is None else handler_factory(handlers))
    toggle = keymap.get("reasoning_toggle", ())
    scrollback.toggle_key = msgs.describe_keys(toggle[0]) if toggle else ""

    def _status_text():
        try:
            return ANSI(status_bar.accent_clock(status(get_app().output.get_size().columns)))
        except Exception:  # noqa: BLE001 — the bar must never take the screen down
            return " "

    def _style() -> Style:
        try:
            colours = status_colours()
        except Exception:  # noqa: BLE001
            colours = ""
        return status_bar.status_style_sheet(colours)

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
                windows.container(lambda shown: Window(
                    BufferControl(buffer=shown.buffer, lexer=screen_scrollback.AnsiLexer(), focusable=False), wrap_lines=True)),
                Window(FormattedTextControl(_status_text), height=1, style="class:status"),
                Window(BufferControl(buffer=input_buffer,
                                     input_processors=[HighlightIncrementalSearchProcessor()],
                                     search_buffer_control=search_toolbar.control,
                                     lexer=None),
                       # A long line wraps; the box grows to half the screen before it scrolls.
                       height=_input_height, dont_extend_height=True, wrap_lines=True,
                       get_line_prefix=lambda lineno, wrap: to_formatted_text(ANSI(_prompt() if lineno == 0 and not wrap
                                                                                   else " " * text_layout.cells(_prompt())))),
                ConditionalContainer(
                    Window(FormattedTextControl(_exit_hint), height=1),
                    filter=Condition(lambda: bool(armed)),
                ),
                ConditionalContainer(
                    Window(BufferControl(buffer=ex_buffer), height=1, get_line_prefix=lambda *_: ":"),
                    filter=has_focus(ex_buffer),
                ),
                search_toolbar,
            ], style=status_bar.screen_style),
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


class capture_stdio:
    """Route sys.stdout/sys.stderr into the scrollback while the app runs."""

    def __init__(self, loop, scrollback: screen_scrollback.Scrollback, app: Application) -> None:
        self._proxy = _ScreenStdout(loop, scrollback, app, sys.__stdout__)

    def __enter__(self):
        self._saved = (sys.stdout, sys.stderr)
        sys.stdout = sys.stderr = self._proxy
        return self

    def __exit__(self, *exc) -> None:
        sys.stdout, sys.stderr = self._saved
