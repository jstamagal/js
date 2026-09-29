"""The async REPL's screen from open to close: what is in the scrollback before
the first prompt, and what happens to a turn still streaming at exit or EOF."""

from __future__ import annotations

import asyncio
import io
import json
import re

import pytest

from js import cli, runtime
from js.config import from_env
from js.memory import load_messages, load_replay_messages
from repl_driver import LineSession, _telemetry, repl_state

SGR = re.compile(r"\x1b\[[0-9;]*m")


class _Recorder(io.StringIO):
    """The terminal: records what reaches it, and what reaches it after the
    screen has closed."""

    def __init__(self) -> None:
        super().__init__()
        self.closed_screen = False
        self.after_close = ""

    def write(self, text: str) -> int:
        if self.closed_screen:
            self.after_close += text
        return super().write(text)

    def isatty(self) -> bool:
        return True


class _Screen:
    """Stands in for the prompt_toolkit app: `run_async` runs `script`, then
    returns once `exit()` is called, as the real app leaves its screen."""

    def __init__(self, script, on_line, on_eof, scrollback, terminal: _Recorder) -> None:
        self.script, self.on_line, self.on_eof = script, on_line, on_eof
        self.scrollback, self.terminal = scrollback, terminal
        self.exited: asyncio.Event | None = None
        self.exits = 0

    async def run_async(self) -> None:
        self.exited = asyncio.Event()
        await self.script(self)
        await self.exited.wait()
        self.terminal.closed_screen = True

    def exit(self) -> None:
        self.exits += 1
        self.exited.set()

    def invalidate(self) -> None:
        pass

    async def scrollback_text(self) -> str:
        for _ in range(5):
            await asyncio.sleep(0)
        self.scrollback.flush()
        return SGR.sub("", self.scrollback.buffer.text)


def _install_screen(monkeypatch, script, terminal: _Recorder) -> list[_Screen]:
    screens: list[_Screen] = []

    def build_app(*, on_line, on_eof, **_kwargs):
        scrollback = cli.screen.Scrollback()
        screens.append(_Screen(script, on_line, on_eof, scrollback, terminal))
        return screens[-1], scrollback

    monkeypatch.setattr(cli.screen, "build_app", build_app)
    monkeypatch.setattr(cli.sys, "stdout", terminal)
    monkeypatch.setattr(cli.sys, "stderr", terminal)
    return screens


def _session_file(tmp_path):
    found = list((tmp_path / ".js" / "sessions").rglob("*.jsonl"))
    assert len(found) == 1, found
    return found[0]


def _marks(session_file) -> list[str]:
    marks = []
    for line in session_file.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if record.get("kind") == "mark":
            marks.append(record.get("marker"))
    return marks


# --------------------------------------------------------------------------
# Before the first prompt
# --------------------------------------------------------------------------

def test_resume_notices_and_last_exchanges_are_in_the_scrollback_before_the_prompt(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)

    def answer(cfg, system, messages, telemetry, **kwargs):
        messages.append({"role": "assistant", "content": f"answer to {messages[-1]['content']}"})

    monkeypatch.setattr(cli.runtime, "run_turn", answer)
    monkeypatch.setattr(cli, "PromptSession", lambda *a, **k: LineSession(["one", "two", "three", "four"]))
    assert cli.main(["--blocking", "--model", "cliapiproxy/claude-opus-5"]) == 0
    session_file = _session_file(tmp_path)

    seen: list[str] = []

    async def script(screen):
        seen.append(await screen.scrollback_text())
        await screen.on_eof()

    terminal = _Recorder()
    _install_screen(monkeypatch, script, terminal)
    monkeypatch.setattr(cli, "PromptSession", lambda *a, **k: LineSession([]))
    assert cli.main(["--session", session_file.stem, "--extra", "ui.resume_exchanges=2"]) == 0

    shown = seen[0]
    assert "Model: cliapiproxy/claude-opus-5" in shown
    assert "Resumed: 8 prior messages" in shown
    assert "one" not in shown.split("Resumed")[1]
    for line in ("three", "answer to three", "four", "answer to four"):
        assert line in shown
    assert shown.index("three") < shown.index("answer to three") < shown.index("four") < shown.index("answer to four")
    assert "Resumed" not in terminal.getvalue()


def test_an_empty_session_says_so_in_the_scrollback(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli, "PromptSession", lambda *a, **k: LineSession([]))
    seen: list[str] = []

    async def script(screen):
        seen.append(await screen.scrollback_text())
        await screen.on_eof()

    terminal = _Recorder()
    _install_screen(monkeypatch, script, terminal)
    assert cli.main(["--session", "nothing-here"]) == 0

    assert "Empty session" in seen[0]
    assert "Empty session" not in terminal.getvalue()


# --------------------------------------------------------------------------
# exit or EOF while a turn streams
# --------------------------------------------------------------------------

@pytest.mark.parametrize("how", ["exit", "eof"])
def test_quitting_mid_turn_interrupts_it_and_writes_nothing_after_the_screen_closes(monkeypatch, tmp_path, how):
    monkeypatch.chdir(tmp_path)
    streaming = asyncio.Event()

    async def stream(**kwargs):
        on_text = kwargs.get("on_text")
        for word in ("The ", "answer ", "so ", "far"):
            on_text(word)
        streaming.set()
        await asyncio.sleep(30)

    monkeypatch.setattr(runtime.model_client, "stream_model_async", stream)

    async def script(screen):
        await screen.on_line("tell me everything")
        await asyncio.wait_for(streaming.wait(), 10)
        if how == "exit":
            await screen.on_line("exit")
        else:
            await screen.on_eof()

    terminal = _Recorder()
    screens = _install_screen(monkeypatch, script, terminal)
    cfg = from_env()
    state, prompt_spec = repl_state(cfg)
    assert runtime.model_client.run_owning_loop(
        cli._repl_main(cfg, state, _telemetry(cfg, state), LineSession([]), prompt_spec)
    ) == 0

    assert screens[0].exits == 1
    assert terminal.after_close == ""
    shown = SGR.sub("", screens[0].scrollback.buffer.text)
    assert "The answer so far" in shown
    assert "interrupted" in shown.lower()

    session_file = _session_file(tmp_path)
    assert "turn_interrupted" in _marks(session_file)
    saved = load_messages(session_file)
    assert [m["role"] for m in saved] == ["user", "assistant"]
    assert saved[1]["content"] == "The answer so far"
    replay = [m["role"] for m in load_replay_messages(session_file)]
    assert all(a != b for a, b in zip(replay, replay[1:])), replay
