"""A bracketed paste over 10 lines or 1000 characters shows as one marker in
the input line, and the line sends the full text."""

from __future__ import annotations

import asyncio
import json

from prompt_toolkit.application import create_app_session
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from js import pastes, screen, settings
from js import prompt_history as ph

START, END = "\x1b[200~", "\x1b[201~"


def _paste_then_enter(text: str, live_settings: dict | None = None) -> tuple[list[str], str]:
    """Paste ``text`` into a real screen, press Enter; the lines sent and the scrollback."""
    lines: list[str] = []

    async def on_line(line: str) -> None:
        lines.append(line)

    async def main() -> str:
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            app, scrollback = screen.build_app(
                prompt="> ", history=InMemoryHistory(), completer=None,
                on_line=on_line, on_interrupt=lambda: None, on_eof=lambda: None,
                key_bindings=pastes.key_bindings(lambda: live_settings),
            )
            task = asyncio.ensure_future(app.run_async())
            await asyncio.sleep(0.05)
            pipe.send_text("see " + START + text + END)
            await asyncio.sleep(0.05)
            pipe.send_text("\r")
            await asyncio.sleep(0.2)
            app.exit()
            await task
            scrollback.flush()
            return scrollback.buffer.text

    shown = asyncio.run(main())
    return lines, shown


def test_a_paste_over_ten_lines_shows_one_marker_and_sends_the_full_text():
    text = "\n".join(f"line {i}" for i in range(20))

    lines, shown = _paste_then_enter(text)

    assert lines == [f"see {text}"]
    assert "line 5" not in shown
    assert "+20 lines]" in shown
    assert shown.count("[paste #") == 1


def test_a_paste_over_1000_chars_on_one_line_shows_a_char_count_marker():
    text = "x" * 1500

    lines, shown = _paste_then_enter(text)

    assert lines == [f"see {text}"]
    assert "1500 chars]" in shown
    assert "x" * 50 not in shown


def test_a_small_paste_goes_in_as_typed():
    text = "one\ntwo\nthree"

    lines, shown = _paste_then_enter(text)

    assert lines == [f"see {text}"]
    assert "[paste #" not in shown


def test_limits_come_from_the_settings():
    live = settings.seed_defaults()
    settings.set_dotted(live, ("ui", "paste_collapse_lines"), 2)
    lines, shown = _paste_then_enter("a\nb\nc", live)
    assert lines == ["see a\nb\nc"]
    assert "+3 lines]" in shown

    settings.set_dotted(live, ("ui", "paste_collapse_lines"), 0)
    settings.set_dotted(live, ("ui", "paste_collapse_chars"), 0)
    text = "\n".join(str(i) for i in range(50))
    _lines, shown = _paste_then_enter(text, live)
    assert "[paste #" not in shown


def test_carriage_returns_in_a_paste_become_newlines():
    text = "\r\n".join(f"row {i}" for i in range(12))

    lines, _shown = _paste_then_enter(text)

    assert lines == ["see " + text.replace("\r\n", "\n")]


def test_expand_is_one_pass_and_leaves_unknown_markers():
    inner = pastes.keep("[paste #999 +3 lines]", 10)

    assert pastes.expand(f"a {inner} b") == "a [paste #999 +3 lines] b"
    assert pastes.expand("[paste #999 +3 lines]") == "[paste #999 +3 lines]"


def test_history_file_keeps_the_full_paste(tmp_path):
    text = "\n".join(f"line {i}" for i in range(15))
    token = pastes.keep(text, 10)
    path = tmp_path / "history.jsonl"
    history = ph.PromptHistory(path, lambda: ph.Origin(cwd="/p", session="s", agent="a"))

    history.store_string(f"look {token}")

    [record] = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert record["text"] == f"look {text}"


def test_a_large_paste_into_the_ex_line_goes_in_as_text():
    ex: list[str] = []

    async def on_ex(text: str, editor: screen.InputEditor) -> None:
        ex.append(text)

    async def main() -> None:
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            app, _scrollback = screen.build_app(
                prompt="> ", history=InMemoryHistory(), completer=None,
                on_line=lambda line: asyncio.sleep(0), on_interrupt=lambda: None, on_eof=lambda: None,
                editing_mode=lambda: "vi", on_ex=on_ex,
                key_bindings=pastes.key_bindings(lambda: None),
            )
            task = asyncio.ensure_future(app.run_async())
            for chunk in ("\x1b", ":echo ", START + "y" * 1500 + END, "\r"):
                await asyncio.sleep(0.05)
                pipe.send_text(chunk)
            await asyncio.sleep(0.2)
            app.exit()
            await task

    asyncio.run(main())

    assert ex == ["echo " + "y" * 1500]


def test_only_the_input_line_collapses_a_paste():
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.enums import DEFAULT_BUFFER, SEARCH_BUFFER

    text = "\n".join(f"line {i}" for i in range(20))
    for name, collapsed in ((DEFAULT_BUFFER, True), (SEARCH_BUFFER, False), ("", False)):
        buffer = Buffer(name=name, multiline=True)
        pastes.insert(buffer, text, None)
        assert (buffer.text != text) == collapsed, name
        assert pastes.expand(buffer.text) == text
