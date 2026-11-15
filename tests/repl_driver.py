"""Run the REPL headless on a given Config: a stub screen feeds the lines.

`stub_screen` replaces the screen with one that hands each line to the REPL's
Enter handler and then sends EOF. `run_async` drives `cli._repl_main` over it
and returns the session's state; a test that starts js through `cli.main`
calls `stub_screen` itself. `turn_stub` stands a plain function in for the
turn the REPL awaits.
"""

from __future__ import annotations

import sys

from js import cli, model_client, runtime
from js import persona as P


def turn_stub(turn):
    """`turn(cfg, system, messages, telemetry, **kwargs)` as the coroutine the
    REPL awaits in place of `runtime.run_turn_async`."""
    async def run_turn_async(*args, **kwargs):
        return turn(*args, **kwargs)

    return run_turn_async


def repl_state(cfg, **state_kwargs):
    """The state `main` would start the REPL with, and the agent's prompt spec."""
    prompt_spec = P.load_configured_prompt_spec(cfg)
    return cli._repl_state(cfg, prompt_spec, **state_kwargs), prompt_spec


def _telemetry(cfg, state):
    telemetry = runtime.Telemetry(debug_log=cfg.debug_log)
    cli._sync_telemetry_from_live_settings(cfg, state, telemetry)
    return telemetry


def stub_screen(monkeypatch, lines, *, wait: bool = False) -> None:
    """Each line reaches the REPL's Enter handler in order, then EOF once
    every turn they started has ended (EOF cancels a running turn). The lines
    arrive back to back, so one typed behind a turn queues or steers; with
    `wait` each line is sent once the turns before it have ended, as at an
    idle prompt.

    `lines` may instead be an async function; it is called with the Enter
    handler and EOF follows when it returns and the turns have ended."""
    queues = []
    turn_consumer = cli._turn_consumer

    def recording_consumer(queue, *args, **kwargs):
        queues.append(queue)
        return turn_consumer(queue, *args, **kwargs)

    class AppStub:
        def __init__(self, on_line, on_eof):
            self._on_line, self._on_eof = on_line, on_eof

        async def run_async(self):
            if callable(lines):
                await lines(self._on_line)
            else:
                for line in lines:
                    await self._on_line(line.strip())
                    if wait:
                        await self._idle()
            await self._idle()
            self._on_eof()

        async def _idle(self):
            for queue in queues:
                await queue.join()

        def exit(self):
            pass

        def invalidate(self):
            pass

    class VisibleScrollback(cli.screen.Scrollback):
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

    def build_app_stub(*, on_line, on_eof, **_kwargs):
        return AppStub(on_line, on_eof), VisibleScrollback(sys.stdout)

    monkeypatch.setattr(cli, "_turn_consumer", recording_consumer)
    monkeypatch.setattr(cli.screen, "build_app", build_app_stub)


def run_async(monkeypatch, cfg, lines, *, wait: bool = False, **state_kwargs) -> dict:
    """Run the REPL over `lines` (see `stub_screen`) and return its state."""
    stub_screen(monkeypatch, lines, wait=wait)
    state, prompt_spec = repl_state(cfg, **state_kwargs)
    assert model_client.run_owning_loop(
        cli._repl_main(cfg, state, _telemetry(cfg, state), None, None, prompt_spec)
    ) == 0
    return state
