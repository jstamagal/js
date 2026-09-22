"""Three-region screen for the async REPL: scrollback, status bar, input line.

Output never touches the input line because it is a different window. Every
`sys.stdout`/`sys.stderr` write during the REPL is marshalled onto the loop and
appended to the scrollback buffer; the input buffer is untouched.
"""
from __future__ import annotations

import asyncio
import os
import re
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

from .context_budget import estimate_text_tokens
from .reasoning_display import grey

STATUS_STYLE = "bold #ffffff bg:#1b3a6b"
SCROLLBACK_LINES = 5000

# Skin-tone modifiers and variation selectors: the Linux console cannot draw
# them, and prompt_toolkit's width for the sequence disagrees with fbcon's, so
# every column after one is off and the wrapper re-breaks the line on each
# repaint. Dropped on TERM=linux; the base glyph stays.
_CONSOLE_UNDRAWABLE = re.compile("[\U0001F3FB-\U0001F3FF\uFE0E\uFE0F]")
_ON_CONSOLE = os.environ.get("TERM") == "linux"


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
        self._reasoning: list[ReasoningBlock] = []

    def append(self, text: str) -> None:
        if _ON_CONSOLE:
            text = _CONSOLE_UNDRAWABLE.sub("", text)
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
        self._set_text(text, doc.cursor_position, follow=doc.is_cursor_at_the_end)

    def _set_text(self, text: str, cursor: int, *, follow: bool) -> None:
        lines = text.split("\n")
        trimmed = "\n".join(lines[-SCROLLBACK_LINES:])
        removed = len(text) - len(trimmed)
        if removed:
            for block in self._reasoning:
                block.start = max(0, block.start - removed)
                block.end = max(0, block.end - removed)
            self._reasoning[:] = [block for block in self._reasoning if block.end > 0]
        self.buffer.set_document(
            Document(trimmed, len(trimmed) if follow else max(0, min(cursor - removed, len(trimmed)))),
            bypass_readonly=True,
        )

    def flush(self) -> None:
        self._commit()

    def reasoning(self, level: int) -> ReasoningBlock:
        self.flush()
        block = ReasoningBlock(self, level, len(self.buffer.text))
        self._reasoning.append(block)
        return block

    def _render_reasoning(self, block: ReasoningBlock, *, new_text: bool = False) -> None:
        self.flush()
        doc = self.buffer.document
        if block not in self._reasoning:
            if not new_text:
                return
            block.start = block.end = len(doc.text)
            self._reasoning.append(block)
        rendered = block.render()
        if _ON_CONSOLE:
            rendered = _CONSOLE_UNDRAWABLE.sub("", rendered)
        end = block.end
        change = len(rendered) - (end - block.start)
        text = doc.text[:block.start] + rendered + doc.text[end:]
        for following in self._reasoning:
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
        blocks = [block for block in self._reasoning if block.level and block.text]
        collapse = not any(block.collapsed for block in blocks)
        for block in blocks:
            block.manual = True
            block.collapsed = collapse
            self._render_reasoning(block)
        return bool(blocks)


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
            return grey(heading + "  Ctrl-R to expand\n")
        return grey(heading + "\n" + self.text + ("" if self.text.endswith("\n") else "\n"))

    def append(self, text: str) -> None:
        self.text += text
        self.owner._render_reasoning(self, new_text=True)

    def answer_started(self) -> None:
        if self.level == 1 and not self.manual and not self.collapsed:
            self.collapsed = True
            self.owner._render_reasoning(self)

    def finish(self, tokens: int | None = None) -> None:
        self.tokens = tokens
        self.answer_started()
        self.owner._render_reasoning(self)


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

    @kb.add("c-r")
    def _ctrl_r(event) -> None:
        if scrollback.toggle_reasoning():
            event.app.invalidate()

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
