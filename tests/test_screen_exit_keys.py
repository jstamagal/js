"""The interrupt and eof keys on the async screen, typed into a real app: when
they clear the input line, when they arm the exit, and when they quit."""

from __future__ import annotations

import asyncio

from prompt_toolkit.application import create_app_session
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from js import screen

CTRL_C, CTRL_D, CTRL_B = "\x03", "\x04", "\x02"


def _drive(chunks: list[str], *, turn_running: bool = False) -> tuple[int, str]:
    """Type ``chunks`` (one per write) into a real app: how many times it quit,
    and the input line left at the end."""
    quits: list[int] = []

    async def main() -> str:
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            app, _scrollback = screen.build_app(
                prompt="> ", history=InMemoryHistory(), completer=None,
                on_line=lambda line: asyncio.sleep(0),
                on_interrupt=lambda: turn_running,
                on_eof=lambda: quits.append(1),
            )
            task = asyncio.ensure_future(app.run_async())
            for chunk in chunks:
                await asyncio.sleep(0.05)
                pipe.send_text(chunk)
            await asyncio.sleep(0.2)
            text = app.layout.current_buffer.text
            app.exit()
            await task
            return text

    text = asyncio.run(main())
    return len(quits), text


def test_interrupt_clears_a_line_with_text_and_a_second_press_quits():
    assert _drive(["hello", CTRL_C]) == (0, "")
    assert _drive(["hello", CTRL_C, CTRL_C]) == (1, "")


def test_interrupt_on_an_empty_line_arms_the_exit():
    assert _drive([CTRL_C]) == (0, "")
    assert _drive([CTRL_C, CTRL_C]) == (1, "")


def test_a_second_interrupt_after_the_window_does_not_quit(monkeypatch):
    monkeypatch.setattr(screen, "EXIT_ARM_SECONDS", 0.0)

    assert _drive(["hello", CTRL_C, CTRL_C]) == (0, "")


def test_interrupt_while_a_turn_runs_never_quits_and_keeps_the_line():
    assert _drive(["hello", CTRL_C, CTRL_C], turn_running=True) == (0, "hello")


def test_eof_at_the_end_of_a_line_with_text_arms_the_exit():
    assert _drive(["hello", CTRL_D]) == (0, "hello")
    assert _drive(["hello", CTRL_D, CTRL_D]) == (1, "hello")


def test_eof_inside_a_line_deletes_the_next_character():
    assert _drive(["hello", CTRL_B, CTRL_D, CTRL_D]) == (0, "hell")


def test_eof_on_an_empty_line_quits():
    assert _drive([CTRL_D]) == (1, "")
