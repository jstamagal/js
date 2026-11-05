"""ircII-style windows on the async REPL screen: a subagent launched from the
REPL gets window 2, its output lands there and marks it active, a line typed
there reaches the subagent, and window 1 keeps the main conversation."""

from __future__ import annotations

import asyncio
import re

from prompt_toolkit.application import create_app_session
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from js import cli, screen
from js.config import from_env
from js.memory import load_messages
from js.toolkit import ToolContext, meta
from repl_driver import run_async

SGR = re.compile(r"\x1b\[[0-9;]*m")


def _text(scrollback: screen.Scrollback) -> str:
    scrollback.flush()
    return SGR.sub("", scrollback.buffer.text)


async def _until(check, timeout: float = 5.0) -> None:
    for _ in range(int(timeout / 0.01)):
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out")


async def _settle() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


def _worker(tmp_path) -> None:
    agent = tmp_path / ".js" / "agents" / "worker"
    agent.mkdir(parents=True)
    (agent / "agent.yaml").write_text("tools: []\n", encoding="utf-8")
    (agent / "01-prompt.md").write_text("WORKER\n", encoding="utf-8")


def test_a_subagent_gets_a_window_that_shows_its_output_and_takes_typed_lines(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_DEFAULT_AGENT", raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)
    monkeypatch.chdir(tmp_path)
    _worker(tmp_path)
    child_turns: list[list[str]] = []
    child_sessions = []
    seen: dict = {}

    async def turn(cfg, system, messages, telemetry, **kwargs):
        if cfg.agent_id == "worker":
            child_sessions.append(cfg.session_file)
            print("child streamed this")
            steer = kwargs.get("steer")
            if not child_turns:
                # The first run stays open until a line typed in its window
                # arrives at a tool boundary.
                steered = None
                while steered is None:
                    await asyncio.sleep(0.01)
                    steered = steer()
                messages.append(steered)
            child_turns.append([str(m.get("content")) for m in messages if m.get("role") == "user"])
            messages.append({"role": "assistant", "content": f"child reply {len(child_turns)}"})
            return
        print("main says hello")
        context = ToolContext(cwd=tmp_path)
        context.config = cfg
        seen["result"] = await meta.task_async(tasks=["work"], agent_id="worker", context=context)
        messages.append({"role": "assistant", "content": "main done"})

    async def script(on_line):
        windows = meta.window_opener.get().__self__
        await on_line("launch a worker")
        await _until(lambda: windows.windows.view(2) is not None and windows.windows.activity() == [2])
        main = windows.windows.view(1).scrollback
        child = windows.windows.view(2)
        await _settle()
        before = _text(main)
        assert "main says hello" in before
        assert "child streamed this" not in before

        windows.windows.switch(2)
        assert windows.windows.activity() == []
        assert windows.windows.scrollback() is child.scrollback
        await _settle()
        assert "child streamed this" in _text(child.scrollback)

        await on_line("hello child")
        await _until(lambda: "result" in seen)
        assert seen["result"] == "child reply 1"

        await on_line("and again")
        await _until(lambda: len(child_turns) == 2 and not child.owner.busy())

        windows.windows.switch(1)
        assert windows.windows.scrollback() is main
        await _settle()
        assert "child streamed this" not in _text(main)
        assert "hello child" not in _text(main)

    monkeypatch.setattr(cli.runtime, "run_turn_async", turn)
    run_async(monkeypatch, from_env(), script)

    assert child_turns[0] == ["work", "hello child"]
    assert child_turns[1] == ["work", "hello child", "and again"]
    # Both of the window's turns ran on the subagent's own session.
    assert len(set(child_sessions)) == 1
    saved = [m.get("content") for m in load_messages(child_sessions[0])]
    assert "hello child" in saved and "and again" in saved and "child reply 2" in saved


def _drive(keys: list[str], windows: screen.Windows) -> list[str]:
    lines: list[str] = []

    async def on_line(line: str) -> None:
        lines.append(line)

    async def main() -> None:
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            app, _scrollback = screen.build_app(
                prompt="> ", history=InMemoryHistory(), completer=None,
                on_line=on_line, on_interrupt=lambda: None, on_eof=lambda: None, windows=windows,
            )
            windows.open()
            task = asyncio.ensure_future(app.run_async())
            for chunk in keys:
                await asyncio.sleep(0.05)
                pipe.send_text(chunk)
            await asyncio.sleep(0.2)
            app.exit()
            await task

    asyncio.run(main())
    return lines


def test_ctrl_w_then_a_digit_shows_that_window():
    windows = screen.Windows()

    _drive(["\x17", "2"], windows)

    assert windows.current == 2


def test_ctrl_w_then_another_key_still_deletes_the_word_before_the_cursor():
    windows = screen.Windows()

    lines = _drive(["hello world", "\x17", "x\r"], windows)

    assert lines == ["hello x"]
    assert windows.current == 1


def test_output_to_a_hidden_window_marks_it_until_it_is_shown():
    windows = screen.Windows()
    windows.set_main(screen.Scrollback())
    two, three = windows.open(), windows.open()

    three.scrollback.append("hi\n")
    three.scrollback.flush()
    two.scrollback.append("there\n")
    two.scrollback.flush()

    assert (two.number, three.number) == (2, 3)
    assert windows.activity() == [2, 3]
    windows.switch(3)
    assert windows.activity() == [2]
    windows.switch(1)
    assert windows.activity() == [2]


def test_window_numbers_stop_at_nine_and_a_closed_number_is_reused():
    windows = screen.Windows()
    windows.set_main(screen.Scrollback())
    opened = [windows.open() for _ in range(9)]

    assert [view.number for view in opened[:8]] == list(range(2, 10))
    assert opened[8] is None
    assert windows.close(1) is None
    windows.switch(4)
    assert windows.close(4) is opened[2]
    assert windows.current == 1
    assert windows.open().number == 4


def test_the_status_bar_names_the_window_and_the_active_ones():
    line = screen.status_line(
        120, clock="12:00", provider="p", model="m", context_tokens=None, phase="", output_tokens=None,
        throbber="", agent_id="a", session_short="s", cache_pct=None, window=2, activity=[3, 5])

    assert "[2]" in line
    assert "3,5" in line


def test_prints_in_a_writing_context_land_in_that_window(monkeypatch):
    async def main():
        loop = asyncio.get_running_loop()
        main_sb, other = screen.Scrollback(), screen.Scrollback()

        class App:
            def invalidate(self):
                pass

        out = screen._ScreenStdout(loop, main_sb, App(), None)
        out.write("one\n")
        with screen.writing(other):
            out.write("two\n")
        out.write("three\n")
        await _settle()
        return _text(main_sb), _text(other)

    main_text, other_text = asyncio.run(main())

    assert main_text == "one\nthree\n"
    assert other_text == "two\n"


def test_a_subagent_launched_with_every_window_open_says_so(monkeypatch):
    from js import console, output

    err = []
    monkeypatch.setattr(console.msgs, "say", lambda message, **fields: err.append((message, fields)))
    launch = {"agent": "w", "kind": "task", "index": 1, "total": 1, "model": "m", "provider": "p",
              "route": "p/m", "effort": "", "tools": 0, "session": "", "session_file": ""}

    for window in (None, 0, 4):
        console.ConsoleSink(None).emit(output.event("subagent_start", **launch, window=window))

    holes = [fields["window"] for _message, fields in err]
    assert holes[0] == ""
    assert "9" in holes[1]
    assert "4" in holes[2]

