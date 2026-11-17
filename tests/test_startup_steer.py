"""A line typed while the agent's startup exchanges run is not lost."""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace

from js import cli


def test_a_line_typed_during_startup_exchanges_is_sent_when_they_end(monkeypatch):
    ran: list[str] = []
    inbox: list[str] = []

    async def run_repl_turn(line, *_rest):
        ran.append(line)

    monkeypatch.setattr(cli, "_run_repl_turn", run_repl_turn)
    monkeypatch.setattr(cli, "_steer_mode", lambda _state: "now")

    class Supervisor:
        def spawn(self, coro, **_kwargs):
            return SimpleNamespace(task=asyncio.ensure_future(coro))

    async def startup():
        # What the Enter handler does with a line typed while a turn is active.
        inbox.append("typed during startup")

    async def main():
        queue: asyncio.Queue = asyncio.Queue()
        queue.put_nowait(startup)
        consumer = asyncio.ensure_future(
            cli._turn_consumer(queue, Supervisor(), None, None, None, None, None, inbox))
        await queue.join()
        consumer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await consumer

    asyncio.run(main())
    assert ran == ["typed during startup"]
    assert inbox == []
