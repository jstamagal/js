"""Tab on the async screen's input line: the first Tab fills in what every match
shares and lists the matches in a menu; the next Tab selects one."""

from __future__ import annotations

import asyncio
import io

from prompt_toolkit.application import create_app_session
from prompt_toolkit.data_structures import Size
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output.vt100 import Vt100_Output

from js import replcomplete, screen

KEYS = ["model.context_window", "model.id", "model.reasoning_effort", "limits.max_read_lines"]


def _drive(keys: list[str]) -> tuple[str, list[str], str]:
    """Type ``keys`` into a real 60x25 app. Returns the input text, the matches
    the completion state holds, and everything written to the terminal."""
    completer = replcomplete.JsCompleter(commands=lambda: {"set": "set"}, setting_keys=KEYS)
    written = io.StringIO()
    output = Vt100_Output(written, lambda: Size(rows=25, columns=60), term="xterm-256color")
    result: dict = {}

    async def on_line(_line: str) -> None:
        return None

    async def main() -> None:
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=output):
            app, _scrollback = screen.build_app(
                prompt="> ", history=InMemoryHistory(), completer=completer,
                on_line=on_line, on_interrupt=lambda: None, on_eof=lambda: None,
            )
            task = asyncio.ensure_future(app.run_async())
            for chunk in keys:
                await asyncio.sleep(0.1)
                pipe.send_text(chunk)
            await asyncio.sleep(0.4)
            buffer = app.current_buffer
            state = buffer.complete_state
            result["text"] = buffer.text
            result["matches"] = [c.text for c in state.completions] if state else []
            app.exit()
            await task

    asyncio.run(main())
    return result["text"], result["matches"], written.getvalue()


def test_the_first_tab_fills_the_shared_part_and_lists_the_matches():
    text, matches, terminal = _drive(["/set mod", "\t"])

    assert text == "/set model."
    assert matches == ["context_window", "id", "reasoning_effort"]
    assert "reasoning_effort" in terminal


def test_the_next_tab_selects_a_match():
    text, _matches, _terminal = _drive(["/set mod", "\t", "\t"])

    assert text == "/set model.context_window"
