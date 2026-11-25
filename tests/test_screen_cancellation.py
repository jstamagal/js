"""Escape cancels busy input without consuming modal or prefix bindings."""
from __future__ import annotations

import asyncio

from prompt_toolkit.application import create_app_session
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from js import keys, screen


def _drive(chunks, *, active=True, mode="emacs", prefix=False, completer=None):
    cancelled = []
    redrawn = []

    async def main():
        def interrupt():
            cancelled.append(True)
            return active

        def handlers(defaults):
            if prefix:
                defaults["redraw"] = screen.Handler(lambda event: redrawn.append(True))
            return defaults

        keymap = keys.default_keymap()
        if prefix:
            keymap, errors = keys.parse("bind escape r redraw", "/keys")
            assert not errors
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            app, _ = screen.build_app(
                prompt="> ", history=InMemoryHistory(), completer=completer,
                on_line=lambda line: None, on_interrupt=interrupt, on_eof=lambda: None,
                editing_mode=lambda: mode, turn_active=lambda: active,
                keymap=keymap, handler_factory=handlers,
            )
            app.timeoutlen = 0.05
            app.ttimeoutlen = 0.05
            task = asyncio.create_task(app.run_async())
            try:
                for chunk in chunks:
                    await asyncio.sleep(0.1)
                    pipe.send_text(chunk)
                await asyncio.sleep(0.15)
                assert not task.done()
                text = app.layout.current_buffer.text
                current_mode = app.vi_state.input_mode
                app.exit()
                await task
            finally:
                if not task.done():
                    app.exit()
                    await task
        return text, current_mode

    text, current_mode = asyncio.run(main())
    return cancelled, redrawn, text, current_mode


def test_busy_escape_cancels_without_clearing_input():
    cancelled, _, text, _ = _drive(["unfinished", "\x1b"])
    assert cancelled == [True]
    assert text == "unfinished"


def test_idle_escape_does_not_interrupt_or_clear_input():
    cancelled, _, text, _ = _drive(["unfinished", "\x1b", "\x1b"], active=False)
    assert cancelled == []
    assert text == "unfinished"


def test_registered_escape_prefix_wins_over_bare_escape():
    cancelled, redrawn, _, _ = _drive(["\x1br"], prefix=True)
    assert cancelled == []
    assert redrawn == [True]


def test_builtin_alt_key_is_not_consumed_as_bare_escape():
    cancelled, _, text, _ = _drive(["one two", "\x1bb", "X"])
    assert cancelled == []
    assert text == "one Xtwo"


def test_completion_escape_does_not_cancel_running_turn():
    from prompt_toolkit.completion import WordCompleter

    cancelled, _, _, _ = _drive(["a", "\t", "\x1b"], completer=WordCompleter(["alpha", "apple"]))
    assert cancelled == []


def test_vi_first_escape_leaves_insert_mode_second_cancels():
    from prompt_toolkit.key_binding.vi_state import InputMode

    cancelled, _, text, mode = _drive(["unfinished", "\x1b", "\x1b"], mode="vi")
    assert cancelled == [True]
    assert text == "unfinished"
    assert mode == InputMode.NAVIGATION


def test_ex_escape_closes_ex_line_without_cancelling_turn():
    cancelled, _, text, _ = _drive(["unfinished", "\x1b", ":command", "\x1b"], mode="vi")
    assert cancelled == []
    assert text == "unfinished"


def test_search_escape_does_not_cancel_running_turn():
    cancelled, _, _, _ = _drive(["\x12", "\x1b"])
    assert cancelled == []
