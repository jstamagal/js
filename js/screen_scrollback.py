"""The screen's scrollback: the text it holds, the answer and tool spans redrawn in place, the reasoning blocks that fold, and the lexer that colours it all."""

from __future__ import annotations

import os
import re
from collections.abc import Callable

from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from prompt_toolkit.lexers import Lexer

from . import control_sequences, fset, keys, messages as msgs, text_layout, tool_display
from .reasoning_display import grey, settled_rows

# Variation selectors: prompt_toolkit folds one into the cell before it and
# keeps that cell's width, so U+26A0 U+FE0F (a warning sign) is one column in
# its screen model while a terminal that honours U+FE0F draws it two wide. The
# rest of that row then sits one column right of where the renderer believes
# it is, and a later repaint, which writes only the cells it thinks changed,
# leaves that row's characters showing through the next text drawn there.
# Dropped from all scrollback text; the base glyph stays, and prompt_toolkit
# and the terminal give it the same width.


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


class AnsiLexer(Lexer):
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
        # Called after each change to the buffer's text (`Windows` marks activity with it).
        self.changed: Callable[[], None] | None = None

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
        if self.changed is not None:
            self.changed()

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
        width = text_layout.block_width()
        if text_layout.cells(first) + 2 + text_layout.cells(fset.to_plain(hint)) <= width and not _goes_on(first, rest, width):
            return f"{first}  {text_layout.drawn_rows(hint, width, 'COLOR_DIM')[0]}{sep}{rest}"
        # On rows of its own after the block, wrapped like any row, so a
        # header that wraps stays in one piece.
        rows = text_layout.drawn_rows(hint, max(width - 2, 1), "COLOR_DIM")
        body = self.text.rstrip("\n")
        return body + "".join(f"\n  {row}" for row in rows) + self.text[len(body):]


def _goes_on(first: str, rest: str, width: int) -> bool:
    """Whether the row after ``first`` may be ``first`` word-wrapped onto it:
    its first word would not have fit on ``first``, so a wrap could have put
    it there."""
    following = fset.plain(rest.partition("\n")[0]).removeprefix(tool_display.RAIL).split()
    if not following:
        return False
    return text_layout.cells(first.rstrip(" ")) + 1 + text_layout.cells(following[0]) > width


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
        self._cleaner = control_sequences.StreamCleaner()
        # The finished lines of `text` wrapped, kept between renders: a chunk
        # rewraps only the line still open. (width, length of text wrapped, rows)
        self._wrapped: tuple[int, int, str] = (0, 0, "")
        # The rows of the open line that more text cannot change, tabs
        # expanded: (columns of the line they hold, rows)
        self._open: tuple[int, str] = (0, "")

    def _body(self) -> str:
        """`text` word-wrapped to the window, reusing the rows wrapped before:
        a chunk rewraps only the last row of the open line."""
        width = text_layout.block_width()
        done_width, done, rows = self._wrapped
        if done_width != width:
            done, rows = 0, ""
            self._open = (0, "")
        last = self.text.rfind("\n") + 1
        if last > done:
            rows += text_layout.wrap_words(self.text[done:last - 1], width) + "\n"
            done = last
            self._open = (0, "")
        self._wrapped = (width, done, rows)
        if done == len(self.text):
            return rows
        line = self.text[done:].expandtabs(text_layout.TAB)
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
        return "\n".join(text_layout.drawn_rows(parts, text_layout.block_width(), "COLOR_REASONING")) + "\n"

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
