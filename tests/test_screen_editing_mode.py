"""vi editing mode on the async screen's input line: Esc then `:` opens the ex
line and hands its text to on_ex; Enter never sends in vi mode."""

from __future__ import annotations

import asyncio

from prompt_toolkit.application import create_app_session
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from js import exline, screen


def _drive(keys: list[str], mode: str, ex_runner=None) -> tuple[list[str], list[tuple[str, str]]]:
    """Type ``keys`` (one chunk per write) into a real app, then quit it."""
    lines: list[str] = []
    ex: list[tuple[str, str]] = []

    async def on_line(line: str) -> None:
        lines.append(line)

    async def on_ex(text: str, editor: screen.InputEditor) -> None:
        ex.append((text, editor.text))
        if ex_runner is not None:
            await ex_runner(text, editor, on_line)

    async def main() -> None:
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            app, _scrollback = screen.build_app(
                prompt="> ", history=InMemoryHistory(), completer=None,
                on_line=on_line, on_interrupt=lambda: None, on_eof=lambda: None,
                editing_mode=lambda: mode, on_ex=on_ex,
            )
            task = asyncio.ensure_future(app.run_async())
            for chunk in keys:
                await asyncio.sleep(0.05)
                pipe.send_text(chunk)
            await asyncio.sleep(0.2)
            app.exit()
            await task

    asyncio.run(main())
    return lines, ex


def test_vi_escape_colon_runs_an_ex_line_and_keeps_the_buffer():
    lines, ex = _drive(["hello", "\x1b", ":set model X", "\r"], "vi")

    assert ex == [("set model X", "hello")]
    assert lines == []


def test_vi_enter_is_a_newline_not_a_send():
    lines, ex = _drive(["first\rsecond", "\x1b", ":x", "\r"], "vi")

    assert lines == []
    assert ex == [("x", "first\nsecond")]


def test_emacs_enter_sends_the_line():
    lines, ex = _drive(["hello\r"], "emacs")

    assert lines == ["hello"]
    assert ex == []


def test_vi_x_sends_the_whole_buffer_through_on_line():
    async def runner(text, editor, on_line):
        await exline.run_ex(text, editor, is_command=lambda verb: False, dispatch=on_line)

    lines, _ex = _drive(["first\rsecond", "\x1b", ":x", "\r"], "vi", runner)

    assert lines == ["first\nsecond"]


def test_vi_escape_in_the_ex_line_cancels_it():
    lines, ex = _drive(["hello", "\x1b", ":set model X", "\x1b", "\r"], "vi")

    assert ex == []
    assert lines == []
