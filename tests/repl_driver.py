"""Run the REPL functions headless on a given Config: stub input feeds lines.

`run_blocking` drives `cli._blocking_repl`; `run_async` drives
`cli._repl_main` with the screen replaced by a stub app. Both build the
session's state with `cli._repl_state` and return it after EOF.
"""

from __future__ import annotations

import contextlib

from js import cli, model_client, runtime
from js import persona as P


class LineSession:
    """Stands in for PromptSession: each prompt() returns the next line, then EOF."""

    history = None
    completer = None

    def __init__(self, lines):
        self._lines = iter(lines)

    def prompt(self, *_args, **_kwargs):
        try:
            return next(self._lines)
        except StopIteration:
            raise EOFError from None


def repl_state(cfg, **state_kwargs):
    """The state `main` would start the REPL with, and the agent's prompt spec."""
    prompt_spec = P.load_configured_prompt_spec(cfg)
    return cli._repl_state(cfg, prompt_spec, **state_kwargs), prompt_spec


def _telemetry(cfg, state):
    telemetry = runtime.Telemetry(debug_log=cfg.debug_log)
    cli._sync_telemetry_from_live_settings(cfg, state, telemetry)
    return telemetry


def run_blocking(cfg, lines, **state_kwargs) -> dict:
    """Run the blocking loop over `lines`, or over a session object that has
    its own prompt()."""
    session = lines if hasattr(lines, "prompt") else LineSession(lines)
    state, prompt_spec = repl_state(cfg, **state_kwargs)
    cli._blocking_repl(cfg, state, _telemetry(cfg, state), session, prompt_spec)
    return state


def run_async(monkeypatch, cfg, lines, **state_kwargs) -> dict:
    """Each line reaches the async REPL's Enter handler in order, then EOF.

    `lines` may instead be an async function; it is called with the Enter
    handler and EOF follows when it returns."""

    class AppStub:
        def __init__(self, on_line, on_eof):
            self._on_line, self._on_eof = on_line, on_eof

        async def run_async(self):
            if callable(lines):
                await lines(self._on_line)
            else:
                for line in lines:
                    await self._on_line(line.strip())
            self._on_eof()

        def exit(self):
            pass

        def invalidate(self):
            pass

    def build_app_stub(*, on_line, on_eof, **_kwargs):
        return AppStub(on_line, on_eof), cli.screen.Scrollback()

    monkeypatch.setattr(cli.screen, "build_app", build_app_stub)
    monkeypatch.setattr(cli.screen, "capture_stdio", lambda *a, **k: contextlib.nullcontext())
    state, prompt_spec = repl_state(cfg, **state_kwargs)
    assert model_client.run_owning_loop(
        cli._repl_main(cfg, state, _telemetry(cfg, state), LineSession([]), prompt_spec)
    ) == 0
    return state
