"""Run the REPL headless on a given Config: a stub screen feeds the lines.

`stub_screen` replaces the screen with one that hands each line to the REPL's
Enter handler and then sends EOF. `run_async` drives `repl._repl_main` over it
and returns the session's state; `type_into_js` starts js through `cli.main`
over it. `turn_stub` stands a plain function in for the turn the REPL awaits;
`record_turns` stands in one that records each turn.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import sys
from dataclasses import dataclass

from js import cli, owning_loop, persona as P, repl, runtime, screen
from js.config import Config


def turn_stub(turn):
    """`turn(cfg, system, messages, telemetry, **kwargs)` as the coroutine the
    REPL awaits in place of `runtime.run_turn_async`."""
    async def run_turn_async(*args, **kwargs):
        return turn(*args, **kwargs)

    return run_turn_async


@dataclass
class TurnCall:
    """One turn the REPL started: the Config it ran on, a copy of the messages
    it was given (its own user message last), a copy of the live settings as
    it started, and its keyword arguments."""

    cfg: Config
    messages: list[dict]
    settings: dict
    kwargs: dict


def record_turns(monkeypatch) -> list[TurnCall]:
    """Each turn the REPL starts is recorded in the returned list and answers
    nothing."""
    turns: list[TurnCall] = []

    def turn(cfg, system, messages, *args, **kwargs):
        turns.append(TurnCall(cfg, list(messages), copy.deepcopy(cfg.settings), kwargs))

    monkeypatch.setattr(runtime, "run_turn_async", turn_stub(turn))
    return turns


def repl_state(cfg, **state_kwargs):
    """The state `main` would start the REPL with, and the agent's prompt spec."""
    prompt_spec = P.load_configured_prompt_spec(cfg)
    return repl._repl_state(cfg, prompt_spec, **state_kwargs), prompt_spec


def _telemetry(cfg, state):
    telemetry = runtime.Telemetry(debug_log=cfg.debug_log)
    repl._sync_telemetry_from_live_settings(cfg, state, telemetry)
    return telemetry


def stub_screen(monkeypatch, lines, *, wait: bool = False) -> None:
    """Each line reaches the REPL's Enter handler in order, then EOF once
    every turn they started has ended (EOF cancels a running turn). The lines
    arrive back to back, so one typed behind a turn queues or steers; with
    `wait` each line is sent once the turns before it have ended, as at an
    idle prompt.

    Once the REPL closes its screen, as `exit` or a chosen `/session` does,
    no further line is typed and no EOF follows.

    `lines` may instead be an async function; it is called with the Enter
    handler, and with the ex line's handler `on_ex(line, editor)` as well
    when it takes two arguments. EOF follows when it returns and the turns
    have ended."""
    queues = []
    turn_consumer = repl._turn_consumer

    def recording_consumer(queue, *args, **kwargs):
        queues.append(queue)
        return turn_consumer(queue, *args, **kwargs)

    class AppStub:
        def __init__(self, on_line, on_eof, on_ex):
            self._on_line, self._on_eof, self._on_ex = on_line, on_eof, on_ex
            self._exited = False

        async def run_async(self):
            # The real app draws its first frame before it reads a key, by
            # which time the loop has run the startup output queued on it.
            await asyncio.sleep(0)
            if callable(lines):
                handlers = (self._on_line, self._on_ex)
                await lines(*handlers[:len(inspect.signature(lines).parameters)])
            else:
                for line in lines:
                    if self._exited:
                        break
                    await self._on_line(line.strip())
                    if wait:
                        await self._idle()
            await self._idle()
            if not self._exited:
                self._on_eof()

        async def _idle(self):
            for queue in queues:
                await queue.join()

        def exit(self):
            self._exited = True

        def invalidate(self):
            pass

    class VisibleScrollback(screen.Scrollback):
        """What the screen would show, mirrored to the test's stdout in the
        order the screen receives it: the real `capture_stdio` routes stdout
        through the loop the same way the live answer arrives."""

        def __init__(self, out) -> None:
            super().__init__()
            self._out = out

        def append(self, text: str) -> None:
            self._out.write(text)
            super().append(text)

        def answer_commit(self, rendered: str) -> None:
            self._out.write(rendered)
            super().answer_commit(rendered)

    def build_app_stub(*, on_line, on_eof, on_ex, **_kwargs):
        return AppStub(on_line, on_eof, on_ex), VisibleScrollback(sys.stdout)

    monkeypatch.setattr(repl, "_turn_consumer", recording_consumer)
    monkeypatch.setattr(screen, "build_app", build_app_stub)


def run_async(monkeypatch, cfg, lines, *, wait: bool = False, **state_kwargs) -> dict:
    """Run the REPL over `lines` (see `stub_screen`) and return its state."""
    stub_screen(monkeypatch, lines, wait=wait)
    state, prompt_spec = repl_state(cfg, **state_kwargs)
    assert owning_loop.run_owning_loop(
        repl._repl_main(cfg, state, _telemetry(cfg, state), None, None, prompt_spec)
    ) == 0
    return state


async def settled() -> None:
    """Run what a command queued on the loop before its Enter handler
    returned. The command runs on an executor thread and queues its screen
    writes, and any call it hops onto the loop, from there. When it finishes
    before the loop thread has chained its future (a fast command, a busy
    machine), asyncio completes that future at once, the handler's await does
    not yield, and those writes are still queued when the handler returns."""
    await asyncio.sleep(0)


async def output_of(enter, capsys, line: str) -> str:
    """What typing `line` into the Enter handler `enter` printed (see
    `stub_screen` for a `lines` function)."""
    capsys.readouterr()
    await enter(line)
    await settled()
    return capsys.readouterr().out


def type_into_js(monkeypatch, lines, *argv: str, wait: bool = True) -> int:
    """The exit code of `js ARGV` started at a terminal, its REPL fed `lines`
    (see `stub_screen`), each once the turns before it have ended."""
    stub_screen(monkeypatch, lines, wait=wait)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    return cli.main(list(argv))
