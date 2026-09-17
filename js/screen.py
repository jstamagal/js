"""Three-region screen for the async REPL: scrollback, status bar, input line.

Output never touches the input line because it is a different window. Every
`sys.stdout`/`sys.stderr` write during the REPL is marshalled onto the loop and
appended to the scrollback buffer; the input buffer is untouched.
"""
from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable, Coroutine

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.styles import Style

STATUS_STYLE = "bold #ffffff bg:#1b3a6b"
SCROLLBACK_LINES = 5000


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

    def append(self, text: str) -> None:
        self._pending += text
        if "\n" not in self._pending and len(self._pending) < 200:
            return
        self._commit()

    def _commit(self) -> None:
        if not self._pending:
            return
        doc = self.buffer.document
        text = doc.text + self._pending
        self._pending = ""
        lines = text.split("\n")
        if len(lines) > SCROLLBACK_LINES:
            text = "\n".join(lines[-SCROLLBACK_LINES:])
        follow = doc.cursor_position >= len(doc.text)
        self.buffer.set_document(
            Document(text, len(text) if follow else min(doc.cursor_position, len(text))),
            bypass_readonly=True,
        )

    def flush(self) -> None:
        self._commit()


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


def build_app(
    *,
    prompt: str,
    history,
    completer,
    on_line: Callable[[str], Coroutine],
    on_interrupt: Callable[[], None],
    on_eof: Callable[[], None],
) -> tuple[Application, Scrollback]:
    scrollback = Scrollback()
    input_buffer = Buffer(
        history=history,
        completer=completer,
        complete_while_typing=False,
        enable_history_search=True,
        multiline=False,
    )
    kb = KeyBindings()

    @kb.add("enter")
    async def _enter(event) -> None:
        line = input_buffer.text
        input_buffer.append_to_history()
        input_buffer.reset()
        if line.strip():
            scrollback.append(f"{prompt}{line}\n")
        await on_line(line.strip())

    @kb.add("c-c")
    def _ctrl_c(event) -> None:
        on_interrupt()

    @kb.add("c-d")
    def _ctrl_d(event) -> None:
        if input_buffer.text:
            input_buffer.delete()
            return
        on_eof()
        event.app.exit()

    @kb.add("c-z")
    def _ctrl_z(event) -> None:
        event.app.suspend_to_background()

    @kb.add("c-l")
    def _ctrl_l(event) -> None:
        event.app.renderer.clear()

    @kb.add("pageup")
    def _pageup(event) -> None:
        scrollback.buffer.cursor_up(count=max(1, event.app.output.get_size().rows - 3))

    @kb.add("pagedown")
    def _pagedown(event) -> None:
        scrollback.buffer.cursor_down(count=max(1, event.app.output.get_size().rows - 3))

    @kb.add("tab")
    def _tab(event) -> None:
        b = input_buffer
        if b.complete_state:
            b.complete_next()
        else:
            b.start_completion(select_first=False)

    layout = Layout(
        HSplit([
            Window(BufferControl(buffer=scrollback.buffer, lexer=_AnsiLexer(), focusable=False),
                   wrap_lines=True),
            Window(FormattedTextControl(lambda: " "), height=1, style="class:status"),
            Window(BufferControl(buffer=input_buffer,
                                 input_processors=[],
                                 lexer=None),
                   height=1, get_line_prefix=lambda *_: to_formatted_text(ANSI(prompt))),
        ]),
        focused_element=input_buffer,
    )
    app = Application(
        layout=layout,
        key_bindings=kb,
        style=Style.from_dict({"status": STATUS_STYLE}),
        full_screen=True,
        mouse_support=False,
    )
    return app, scrollback


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
