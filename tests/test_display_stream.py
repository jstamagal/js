"""Answer Display: finished Markdown blocks commit once, the last block is live,
and output that is not a terminal is plain text."""

from __future__ import annotations

import asyncio
import io
import re

from js import cli, display, screen, transcript

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


def test_text_the_throttle_held_back_is_drawn_when_no_more_text_comes():
    live = RecordingLive()

    async def stream_then_go_quiet() -> None:
        sink = display.Display(lambda _text: None, live=live, refresh_s=0.02)
        sink.chunk("text", "swarm-pick unt")
        sink.chunk("text", "ouched.")
        await asyncio.sleep(0.2)

    asyncio.run(stream_then_go_quiet())

    assert "swarm-pick untouched." in ANSI.sub("", live.events[-1][1])


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


def test_line_separator_in_a_paragraph_does_not_cut_the_block():
    """U+2028 survives clean(); markdown-it numbers lines by \\n only, so the
    commit must slice by \\n too or a block is cut in the middle."""
    live = RecordingLive()
    text = "First para has\u2028a line separator\u2028in it.\n\nSecond para.\n\nThird.\n"
    _stream(text, display.Display(lambda _text: None, live=live, refresh_s=0), size=3)
    commits = [ANSI.sub("", t) for kind, t in live.events if kind == "commit"]
    holding = [t for t in commits if "First para" in t]
    assert len(holding) == 1
    assert "in it." in holding[0]
    plain = "".join(commits)
    for words in ("Second para.", "Third."):
        assert plain.count(words) == 1


def test_tool_result_name_is_cleaned_at_every_level():
    evil = "evil\x1b]0;PWNED\x07\x1b[2J"
    for level in (1, 2, 3):
        out = (display.render_exchange(evil, {}, None, level=level, width=80)
               + display.render_exchange(evil, {}, "ok", level=level, width=80))
        assert "PWNED" not in out and "\x1b[2J" not in out and "\x07" not in out, level
        assert "evil" in out


def test_a_tool_message_without_an_id_is_not_shown_under_a_call_without_one():
    messages = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": None, "function": {"name": "shell", "arguments": "{\"command\": \"ls\"}"}}]},
        {"role": "tool", "tool_call_id": None, "content": "ORPHAN_RESULT"},
    ]
    out = display.render_exchanges(messages, 1, width=80, level=3, preview=12, markdown=False)
    assert "ORPHAN_RESULT" not in out


def test_clean_is_linear_on_unterminated_string_openers():
    """`\\x1b]` with no terminator once made clean rescan to the end from
    every opener (quadratic): 200,000 of them would take minutes."""
    assert display.clean("\x1b]" * 200_000 + "\x07tail") == "tail"


# One of every class of sequence the escape parser knows, some cut short.
_SEQUENCES = ["\x1b[31m", "\x1b[?25l", "\x1b[1;2:3m", "\x1b[ q", "\x9b2J", "\x1b(0", "\x1b#8", "\x1bc",
              "\x1b7", "\x1b]0;title\x07", "\x1b]8;;http://x\x1b\\", "\x9d2;t\x9c", "\x1bPq#0;2;0\x1b\\",
              "\x90$qm\x9c", "\x1bX sos \x1b\\", "\x1b^pm\x07", "\x1b_apc\x9c", "\x98sos\x9c", "\x1b",
              "\x1b[", "\x1b]", "\x1bP", "\x18", "\x1a", "\x85", "\x7f", "\r", "\x00"]
_TEXT = ["plain", " ", "\n", "\t", "漢字", "😀", "é", "[", "]", "\\", ";", "0"]


def test_cleaning_in_any_chunking_is_cleaning_the_whole():
    import random

    rng = random.Random(9)
    for _ in range(3000):
        text = "".join(rng.choice(_SEQUENCES if rng.random() < 0.4 else _TEXT) for _ in range(rng.randint(0, 12)))
        whole = display.clean(text)
        assert "\x1b" not in whole and not any(0x80 <= ord(char) <= 0x9f for char in whole), repr(text)
        cuts = sorted(rng.sample(range(len(text) + 1), min(len(text) + 1, rng.randint(0, 6))))
        cleaner = display.StreamCleaner()
        pieces = [text[start:end] for start, end in zip([0, *cuts], [*cuts, len(text)], strict=True)]
        assert "".join(cleaner.feed(piece) for piece in pieces) + cleaner.flush() == whole, (repr(text), cuts)


def test_an_open_tool_exchange_is_redrawn_where_it_stands_after_later_text():
    scrollback = screen.Scrollback()
    scrollback.append("before\n")
    span = scrollback.exchange_open("running\n")
    scrollback.append("after\n")

    scrollback.exchange_update(span, "running\nits output\n")
    scrollback.flush()
    assert scrollback.buffer.text == "before\nrunning\nits output\nafter\n"

    scrollback.exchange_close(span, "finished\n")
    scrollback.flush()
    assert scrollback.buffer.text == "before\nfinished\nafter\n"


def test_markdown_highlights_take_the_accent_colour_not_the_tool_colour():
    from js import fset

    fset.set_entry("COLOR_ACCENT", "#102030")
    fset.set_entry("COLOR_TOOL", "#405060")
    rendered = display.render_markdown("## Heading two\n\n1. first\n2. second\n", 60)
    assert "38;2;16;32;48" in rendered and "38;2;64;80;96" not in rendered


def test_the_agent_mark_hangs_left_of_the_answer_text():
    live = RecordingLive()
    sink = display.Display(lambda _text: None, live=live, refresh_s=0)
    sink.speaker(*display.agent_speaker())
    _stream("First paragraph of the answer.\n\nSecond one.\n\n- a list item\n", sink)
    shown = "".join(text for kind, text in live.events if kind == "commit")
    rows = [row for row in ANSI.sub("", shown).split("\n") if row.strip()]
    assert rows[0].startswith("● First paragraph")
    assert all(row.startswith("  ") for row in rows[1:]), rows
    assert all(len(row) <= live.width() for row in rows)


def test_plain_text_hangs_under_the_mark_too():
    out = io.StringIO()
    sink = display.Display(out.write)
    sink.speaker(*display.agent_speaker())
    _stream("one\ntwo\n\nthree", sink)
    assert ANSI.sub("", out.getvalue()) == "● one\n  two\n\n  three\n"


def _hung_rows(text: str, width: int) -> list[str]:
    rows = [row for row in ANSI.sub("", text).split("\n") if row.strip()]
    assert all(len(row) < width for row in rows), rows
    assert all(row.startswith("  ") for row in rows[1:]), rows
    return rows


WORDS = [f"word{n}" for n in range(40)]


def test_a_long_user_line_wraps_under_its_text_column(monkeypatch):
    monkeypatch.setattr(display, "terminal_width", lambda: 40)
    rows = _hung_rows(display.user_line(" ".join(WORDS)), 40)
    assert [word for row in rows for word in row.split()][1:] == WORDS


def test_a_long_plain_answer_wraps_under_its_text_column(monkeypatch):
    monkeypatch.setattr(display, "terminal_width", lambda: 40)
    out = io.StringIO()
    sink = display.Display(out.write)
    sink.speaker(*display.agent_speaker())
    _stream(" ".join(WORDS) + "\n", sink, size=7)
    rows = _hung_rows(out.getvalue(), 40)
    assert [word for row in rows for word in row.split()][1:] == WORDS


def test_wide_characters_wrap_by_cells_under_the_text_column(monkeypatch):
    from prompt_toolkit.utils import get_cwidth

    monkeypatch.setattr(display, "terminal_width", lambda: 20)
    rows = [row for row in ANSI.sub("", display.user_line("汉字" * 30 + " 中文 结束")).split("\n") if row.strip()]
    assert all(get_cwidth(row) < 20 for row in rows), rows
    assert all(row.startswith("  ") for row in rows[1:]), rows
    assert "".join(row[2:] for row in rows).replace(" ", "") == "汉字" * 30 + "中文结束"


def test_a_plain_answer_breaks_a_wide_run_with_no_spaces_under_the_text_column(monkeypatch):
    from prompt_toolkit.utils import get_cwidth

    monkeypatch.setattr(display, "terminal_width", lambda: 20)
    out = io.StringIO()
    sink = display.Display(out.write)
    sink.speaker(*display.agent_speaker())
    _stream("汉字" * 20 + "\n", sink, size=3)
    rows = [row for row in ANSI.sub("", out.getvalue()).split("\n") if row.strip()]
    assert len(rows) > 1 and all(get_cwidth(row) < 20 for row in rows), rows
    assert all(row.startswith("  ") for row in rows[1:])


def test_word_wrap_time_grows_with_the_text_not_its_square():
    import time

    def cost(size: int) -> float:
        text = ("word " * (size // 5))[:size]
        best = float("inf")
        for _ in range(2):
            start = time.perf_counter()
            display.wrap_words(text, 77)
            best = min(best, time.perf_counter() - start)
        return best

    small, large = cost(16_000), cost(64_000)
    assert large < small * 8, (small, large)  # four times the text: linear is ~4x, quadratic ~16x


def test_streaming_reasoning_rewraps_only_its_open_paragraph(monkeypatch):
    wrapped: list[int] = []
    real = display.wrap_words

    def counting(text: str, width: int) -> str:
        wrapped.append(len(text))
        return real(text, width)

    monkeypatch.setattr(display, "wrap_words", counting)
    scroll = screen.Scrollback()
    block = scroll.reasoning(2)
    text = "".join(f"paragraph {n} " + "x" * 30 + "\n" for n in range(200))
    for start in range(0, len(text), 10):
        block.append(text[start:start + 10])
    assert sum(wrapped) < 8 * len(text), (sum(wrapped), len(text))
    assert transcript.strip_ansi(scroll.buffer.text).count("paragraph") == 200


def test_a_folded_row_and_its_hint_stay_inside_the_window_with_wide_text(monkeypatch):
    from prompt_toolkit.utils import get_cwidth

    monkeypatch.setattr(display, "terminal_width", lambda: 40)
    scroll = screen.Scrollback()
    shell = "shell=bash\nexit=0\n--- stdout ---\n完成了\n"
    folded = display.render_exchange("shell", {"command": "echo " + "汉字" * 9}, shell, level=1)
    whole = display.render_exchange("shell", {"command": "echo " + "汉字" * 9}, shell, level=3)
    span = scroll.exchange_open(folded)
    scroll.exchange_close(span, folded, expanded=whole)
    assert all(get_cwidth(row) < 40 for row in ANSI.sub("", scroll.buffer.text).split("\n")), scroll.buffer.text


def test_a_word_carried_to_a_narrower_row_is_cut_to_fit_it(monkeypatch):
    from prompt_toolkit.utils import get_cwidth

    monkeypatch.setattr(display, "terminal_width", lambda: 40)
    for size in range(28, 40):
        shell = f"shell=bash\nexit=0\n--- stdout ---\n{'X' * size}\n"
        rendered = display.render_exchange("shell", {"command": "make"}, shell, level=1)
        assert all(get_cwidth(row) < 40 for row in ANSI.sub("", rendered).split("\n")), (size, rendered)
    assert display.cut_points("ab " + "x" * 9, 10, 5) == [3, 8, 12]


def test_plain_answer_wrap_time_grows_with_an_unbroken_run_not_its_square(monkeypatch):
    import time

    monkeypatch.setattr(display, "terminal_width", lambda: 40)

    def cost(size: int) -> float:
        best = float("inf")
        for _ in range(2):
            sink = display.Display(io.StringIO().write)
            sink.speaker(*display.agent_speaker())
            start = time.perf_counter()
            _stream("x" * size + "\n", sink, size=4096)
            best = min(best, time.perf_counter() - start)
        return best

    small, large = cost(8_000), cost(32_000)
    assert large < small * 8, (small, large)


def test_the_agent_colour_paints_the_answer_text_as_well_as_the_mark():
    from js import fset

    fset.set_entry("COLOR_AGENT_MARK", "#ffffff")
    fset.set_entry("COLOR_AGENT", "#102030")
    assert "38;2;16;32;48" in display.render_markdown("hello there", 40)
    out = io.StringIO()
    sink = display.Display(out.write)
    sink.speaker(*display.agent_speaker())
    _stream("hello there\n", sink)
    assert "38;2;16;32;48" in out.getvalue()


def test_plain_streaming_shows_each_full_row_of_an_unbroken_run_as_it_arrives(monkeypatch):
    monkeypatch.setattr(display, "terminal_width", lambda: 40)
    for char in ("漢", "x"):
        out = io.StringIO()
        sink = display.Display(out.write)
        sink.speaker(*display.agent_speaker())
        for _ in range(120):
            sink.chunk("text", char)
        shown = ANSI.sub("", out.getvalue())
        assert shown.count(char) >= 60, (char, shown)  # the full rows are out before the turn ends
        sink.finish()
        assert ANSI.sub("", out.getvalue()).count(char) == 120


def test_a_markdown_table_cell_wraps_instead_of_losing_its_tail(monkeypatch):
    monkeypatch.setattr(display, "terminal_width", lambda: 40)
    live = RecordingLive()
    live.width = lambda: 39
    sink = display.Display(lambda _text: None, live=live, refresh_s=0)
    sink.speaker(*display.agent_speaker())
    _stream("| a | b |\n|---|---|\n| short | " + "x" * 26 + "TAIL |\n", sink, size=200)
    shown = "".join(text for kind, text in live.events if kind == "commit")
    assert "TAIL" in "".join(ANSI.sub("", shown).split()) and "…" not in ANSI.sub("", shown)


def test_plain_answer_text_keeps_its_trailing_spaces():
    import io

    out = io.StringIO()
    sink = display.Display(out.write, colour=False)
    sink.speaker(*display.agent_speaker())
    sink.chunk("text", "alpha  \nbeta  ")
    sink.finish()
    assert "alpha  \n" in out.getvalue() and "beta  \n" in out.getvalue(), repr(out.getvalue())


def test_a_space_that_does_not_fit_ends_the_row_and_starts_no_row():
    assert display.wrap_words("aaaa bbbb", 4) == "aaaa\nbbbb"
    assert display.wrap_words("aa  bb cc", 3) == "aa\nbb\ncc"


def test_a_code_block_hangs_every_row_it_paints_under_the_text_column():
    live = RecordingLive()
    sink = display.Display(lambda _text: None, live=live, refresh_s=0)
    sink.speaker(*display.agent_speaker())
    _stream("Intro.\n\n```python\ndef f():\n\n    return 1\n```\n", sink)
    shown = "".join(text for kind, text in live.events if kind == "commit")
    rows = [row for row in shown.split("\n") if ANSI.sub("", row)]
    assert all(row.startswith("  ") for row in rows[1:]), rows


def test_a_single_newline_in_an_answer_starts_a_new_line():
    shown = re.sub(r"\x1b\[[0-9;]*m", "", display.render_markdown("first line\nsecond line\n", 60))
    assert [line.strip() for line in shown.splitlines() if line.strip()] == ["first line", "second line"]
