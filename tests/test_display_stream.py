"""Answer Display: finished Markdown blocks commit once, the last block is live,
and output that is not a terminal is plain text."""

from __future__ import annotations

import io
import re

from js import cli, display, screen

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

ANSWER = """Here are three snippets.

```python
alpha_one = 1
```

Then the second.

```python
beta_two = 2
```

```python
gamma_three = 3
```

Done.
"""


class RecordingLive:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def width(self) -> int:
        return 60

    def update(self, rendered: str) -> None:
        self.events.append(("update", rendered))

    def commit(self, rendered: str) -> None:
        self.events.append(("commit", rendered))


def _stream(text: str, sink: display.Display, size: int = 5) -> None:
    for start in range(0, len(text), size):
        sink.chunk("text", text[start:start + size])
    sink.finish()


def test_three_fenced_blocks_each_render_highlighted_once_and_are_never_redrawn():
    live = RecordingLive()
    _stream(ANSWER, display.Display(lambda _text: None, live=live, refresh_s=0))

    commits = [text for kind, text in live.events if kind == "commit"]
    for name in ("alpha_one", "beta_two", "gamma_three"):
        holding = [text for text in commits if name in ANSI.sub("", text)]
        assert len(holding) == 1, name
        # Highlighted: the committed block carries colour sequences.
        assert ANSI.search(holding[0])
        committed_at = live.events.index(("commit", holding[0]))
        later_updates = [text for kind, text in live.events[committed_at + 1:] if kind == "update"]
        assert not any(name in ANSI.sub("", text) for text in later_updates), name
    # Everything the stream carried ends up committed.
    plain = ANSI.sub("", "".join(commits))
    for words in ("Here are three snippets.", "Then the second.", "Done."):
        assert words in plain


def test_not_a_terminal_writes_plain_bytes_as_they_arrive():
    out = io.StringIO()
    sink = display.Display.for_stream(out)
    assert not sink.pretty

    sink.chunk("text", "**bold** and `code`\x1b[31m")
    assert out.getvalue() == "**bold** and `code`"
    sink.chunk("text", " end")
    sink.finish()

    assert out.getvalue() == "**bold** and `code` end\n"


def test_markdown_off_on_a_terminal_writes_plain_bytes():
    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    out = Tty()
    display.print_answer("# Title\n\ntext", out, markdown=False)

    assert out.getvalue() == "# Title\n\ntext\n"


def test_scrollback_live_answer_is_replaced_until_committed():
    scrollback = screen.Scrollback()
    scrollback.append("before\n")
    scrollback.answer_update("a\n")
    scrollback.answer_update("ab\n")
    scrollback.answer_commit("AB\n")
    scrollback.answer_update("next\n")
    scrollback.append("after\n")
    scrollback.answer_commit("NEXT\n")
    scrollback.flush()

    assert scrollback.buffer.text == "before\nAB\nNEXT\nafter\n"


def test_scrollback_live_answer_follows_a_collapsing_reasoning_block():
    scrollback = screen.Scrollback()
    block = scrollback.reasoning(1)
    block.append("thinking hard\nstill thinking\n")
    scrollback.answer_update("x\n")
    block.answer_started()
    scrollback.answer_update("xy\n")
    scrollback.answer_commit("XY\n")

    text = scrollback.buffer.text
    assert text.endswith("XY\n")
    assert "still thinking" not in text
    assert text.count("XY") == 1
    assert "xy" not in text and "\nx\n" not in text


def test_tty_live_redraws_only_the_rows_it_wrote():
    written: list[str] = []
    live = display.TtyLive(written.append)

    live.update("one\ntwo\n")
    live.commit("final\n")

    # The commit moves up over exactly the two live rows and erases below them.
    assert written[-2] == "\x1b[2F\x1b[J"
    assert written[-1] == "final\n"


def test_print_answer_to_a_pipe_is_plain_bytes():
    out = io.StringIO()

    display.print_answer("# Heading\n\n```python\nx = 1\n```\n\x1b]0;title\x07tail", out)

    assert out.getvalue() == "# Heading\n\n```python\nx = 1\n```\ntail\n"


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


class _Log:
    def __init__(self) -> None:
        self.text = ""

    def write(self, text: str) -> None:
        self.text += text

    def flush(self) -> None:
        pass


def test_debug_log_keeps_the_committed_answer_but_not_the_redraws():
    terminal, log = _Tty(), _Log()
    sink = display.Display.for_stream(cli._StdoutTee(terminal, log))
    sink.refresh_s = 0
    assert sink.pretty

    for piece in ANSWER.split("\n"):
        sink.chunk("text", piece + "\n")
    sink.finish()

    logged = ANSI.sub("", log.text)
    for name in ("alpha_one", "beta_two", "gamma_three", "Done."):
        assert logged.count(name) == 1, name
    # Cursor movement for the live region stays on the terminal.
    assert "\x1b[J" in terminal.getvalue()
    assert "\x1b[J" not in log.text
