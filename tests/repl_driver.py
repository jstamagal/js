"""Run the REPL headless on a given Config: a stub screen feeds the lines.

`stub_screen` replaces the screen with one that hands each line to the REPL's
Enter handler and then sends EOF. `run_async` runs a `repl.Repl` over it and
returns the session's state; `type_into_js` starts js through `cli.main` over
it. `turn_stub` stands a plain function in for the turn the REPL awaits;
`record_turns` stands in one that records each turn.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import sys
import threading
from dataclasses import dataclass

from js import (
    cli,
    owning_loop,
    persona as P,
    repl,
    repl_commands,
    runtime,
    screen,
    screen_scrollback,
)
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
    return repl.live_state(cfg, prompt_spec, **state_kwargs), prompt_spec


def _screen(lines, wait, screen_parts, idle):
    """A `build_screen` for `repl.Repl.run` that types `lines` (see
    `stub_screen`); `idle()` returns once the turns the lines started have
    ended."""

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
                        await idle()
            await idle()
            if not self._exited:
                self._on_eof()

        def exit(self):
            self._exited = True

        def invalidate(self):
            pass

    class VisibleScrollback(screen_scrollback.Scrollback):
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

    def build_screen(*, on_line, on_eof, on_ex, **kwargs):
        if screen_parts is not None:
            screen_parts.update(kwargs, on_line=on_line, on_eof=on_eof, on_ex=on_ex)
        return AppStub(on_line, on_eof, on_ex), VisibleScrollback(sys.stdout)

    return build_screen


_run = repl.Repl.run


def stub_screen(monkeypatch, lines, *, wait: bool = False, screen_parts: dict | None = None) -> None:
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
    sessions: list[repl.Repl] = []

    async def run(session, build_screen=None):
        sessions.append(session)
        return await _run(session, build_screen)

    async def idle():
        for session in sessions:
            await session.idle()

    monkeypatch.setattr(repl.Repl, "run", run)
    monkeypatch.setattr(screen, "build_app", _screen(lines, wait, screen_parts, idle))


def run_async(monkeypatch, cfg, lines, *, wait: bool = False, screen_parts: dict | None = None,
              **state_kwargs) -> dict:
    """Run the REPL over `lines` (see `stub_screen`) and return its state."""
    state, prompt_spec = repl_state(cfg, **state_kwargs)
    session = repl.Repl(cfg, state, prompt_spec)
    return owning_loop.run_owning_loop(session.run(_screen(lines, wait, screen_parts, session.idle)))


def handled_while_a_turn_runs(monkeypatch, cfg, lines, **state_kwargs) -> list[str]:
    """Type a prompt, then each of `lines` while the turn it started still
    runs; the lines the REPL handed to its command handler meanwhile, in
    order. A line it refuses never reaches the handler."""
    started, release = asyncio.Event(), asyncio.Event()
    handled: list[str] = []

    async def turn(*args, **kwargs):
        if not started.is_set():
            started.set()
            await release.wait()

    handle_command = repl_commands.handle_command

    def recording(line, state, cfg):
        if started.is_set() and not release.is_set():
            handled.append(line)
        return handle_command(line, state, cfg)

    async def typing(enter):
        await enter("go")
        await within(started.wait(), "the turn to start")
        for line in lines:
            await enter(line)
        release.set()

    monkeypatch.setattr(runtime, "run_turn_async", turn)
    monkeypatch.setattr(repl_commands, "handle_command", recording)
    run_async(monkeypatch, cfg, typing, **state_kwargs)
    return handled


WAIT_TIMEOUT = 10


async def within(awaitable, what: str, timeout: float = WAIT_TIMEOUT):
    """The result of `awaitable`; the test fails naming `what` when it has
    not finished within `timeout` seconds. Something only a turn's model call
    produces is waited for this way, so a turn that fails fails the test
    instead of hanging it."""
    try:
        return await asyncio.wait_for(awaitable, timeout)
    except TimeoutError:
        raise AssertionError(f"waited {timeout}s for {what}") from None


async def thread_event(event: threading.Event, what: str, timeout: float = WAIT_TIMEOUT) -> None:
    """Wait off the loop for `event`, set on a tool's thread; the test fails
    as with `within` when it is not set within `timeout` seconds."""
    if not await asyncio.to_thread(event.wait, timeout):
        raise AssertionError(f"waited {timeout}s for {what}")


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
    `stub_screen` for a `lines` function). A line typed before it may still
    have writes queued (`settled`); they land before the capture starts."""
    await settled()
    capsys.readouterr()
    await enter(line)
    await settled()
    return capsys.readouterr().out


def type_into_js(monkeypatch, lines, *argv: str, wait: bool = True, screen_parts: dict | None = None) -> int:
    """The exit code of `js ARGV` started at a terminal, its REPL fed `lines`
    (see `stub_screen`), each once the turns before it have ended."""
    stub_screen(monkeypatch, lines, wait=wait, screen_parts=screen_parts)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    return cli.main(list(argv))
