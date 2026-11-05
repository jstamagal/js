"""Subagent windows on the REPL screen.

Each subagent launched from the async REPL gets the next free window
(`screen.Windows`, 2 to 9). Its window shows everything it streams: its launch
and end lines, its answer, reasoning and tool exchanges. A line typed in the
window reaches the subagent: while it runs, the line is queued and delivered
as a user message at its next tool boundary (the runtime's ``steer``); once
it has finished, the line runs a new turn on the subagent's own session with
its own agent, model, effort and tools. That turn stays in the subagent's
session; the result its task returned to the parent stands, and the parent
is not told.

`meta._run_one_task_async` calls `window_opener` (set here for the REPL's
tasks) with the subagent_start fields; the `SubagentWindow` it gets back
supplies the child's telemetry, its steer, and where its prints land.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import Awaitable, Callable
from typing import Any

from . import display, screen
from . import messages as msgs


class SubagentWindow:
    """One subagent's window and the conversation typed into it."""

    def __init__(self, owner: SubagentWindows, view: screen.View, launch: dict) -> None:
        self._owner = owner
        self.view = view
        self.number = view.number
        self.launch = dict(launch)
        # Lines typed while a turn runs, delivered at its next tool boundary.
        self.inbox: list[str] = []
        # The child's ToolContext, for the status bar.
        self.context: Any = None
        # What runs in the window now: the subagent's task, or a turn typed after it.
        self.task: asyncio.Task | None = None
        # Runs one turn on the subagent's session; set once its task has ended.
        self._resume: Callable[[str], Awaitable[None]] | None = None

    @property
    def scrollback(self) -> screen.Scrollback:
        return self.view.scrollback

    def telemetry(self, debug_log: Any) -> Any:
        """A Telemetry whose answer, reasoning and tool exchanges draw in this window."""
        from . import runtime

        loop, app, scrollback = self._owner.loop, self._owner.app, self.scrollback
        out = screen._ScreenStdout(loop, scrollback, app, sys.__stdout__)
        telemetry = runtime.Telemetry(debug_log=debug_log)
        telemetry.reasoning_factory = lambda level: screen.ScreenReasoningDisplay(loop, scrollback, app, level)
        telemetry.display_factory = lambda markdown: display.Display(
            out.write, live=screen.ScreenLive(loop, scrollback, app) if markdown else None, flush=out.flush)
        telemetry.exchange_factory = lambda: screen.ScreenExchange(loop, scrollback, app)
        return telemetry

    def writing(self):
        """Send this context's prints to the window."""
        return screen.writing(self.scrollback)

    def running(self, context: Any) -> None:
        """The subagent's task, the current one, has started in the window."""
        self.task = asyncio.current_task()
        self.context = context

    def steer(self) -> dict | None:
        """The lines typed since the last model call, as one user message."""
        if not self.inbox:
            return None
        text = "\n".join(self.inbox)
        self.inbox.clear()
        return {"role": "user", "content": text}

    def finished(self, resume: Callable[[str], Awaitable[None]]) -> None:
        """The subagent's task has ended; ``resume`` runs a turn on its
        session. Lines still queued run as that turn."""
        self.task = None
        self._resume = resume
        if self.inbox:
            self._start(self.steer()["content"])

    def busy(self) -> bool:
        return self.task is not None and not self.task.done()

    def send(self, text: str) -> None:
        """A line typed in the window: queued while a turn runs, else a new turn."""
        if self.busy() or self._resume is None:
            self.inbox.append(text)
            with self.writing():
                msgs.say(msgs.WINDOW_QUEUED, agent=self.launch["agent"])
            return
        self._start(text)

    def _start(self, text: str) -> None:
        self.task = self._owner.supervisor.spawn(
            self._turns(text), kind="subagent", label=f"window {self.number}").task

    async def _turns(self, text: str) -> None:
        while text:
            with self.writing():
                try:
                    await self._resume(text)
                except Exception as exc:  # noqa: BLE001 - shown in its window like a REPL turn's
                    msgs.warn(msgs.FAILED, error=f"{type(exc).__name__}: {exc}")
            steered = self.steer()
            text = steered["content"] if steered else ""

    def cancel(self) -> bool:
        """Cancel what runs in the window and drop its queued lines; False
        when nothing ran and nothing was queued."""
        dropped = bool(self.inbox)
        self.inbox.clear()
        if self.busy():
            self.task.cancel()
            return True
        return dropped


class SubagentWindows:
    """The REPL screen's subagent windows: `open` is `meta.window_opener`."""

    def __init__(self, windows: screen.Windows, supervisor: Any) -> None:
        self.windows = windows
        self.supervisor = supervisor
        self.loop: asyncio.AbstractEventLoop = supervisor.loop
        self.app: Any = None

    def attach(self, app: Any) -> None:
        self.app = app
        self.windows.invalidate = app.invalidate

    def open(self, launch: dict) -> SubagentWindow | None:
        """A window for the subagent ``launch`` describes; None when windows
        2 to 9 are all open or there is no screen."""
        if self.app is None:
            return None
        view = self.windows.open()
        if view is None:
            return None
        window = SubagentWindow(self, view, launch)
        view.owner = window
        return window

    def on_screen(self) -> SubagentWindow | None:
        """The subagent window on screen; None in window 1."""
        view = self.windows.view()
        return view.owner if view is not None else None

    def close(self, number: int) -> str | None:
        """Close window ``number``: why it stays, or None once it is closed."""
        view = self.windows.view(number)
        if view is None:
            return msgs.NO_SUCH_WINDOW.said(number=number)
        if number == 1:
            return msgs.WINDOW_MAIN_STAYS.said()
        if view.owner is not None and view.owner.busy():
            return msgs.WINDOW_STILL_RUNNING.said(number=number, agent=view.owner.launch["agent"])
        self.windows.close(number)
        return None

    def rows(self) -> list[dict]:
        """One row per open window, for /window."""
        rows = []
        for number in self.windows.numbers():
            view = self.windows.view(number)
            owner = view.owner
            rows.append({
                "number": number,
                "current": number == self.windows.current,
                "active": view.active,
                "agent": owner.launch["agent"] if owner else "",
                "running": owner.busy() if owner else False,
            })
        return rows


@contextlib.contextmanager
def opening(windows: SubagentWindows):
    """Give the subagents this context launches windows from ``windows``."""
    from .toolkit import meta

    token = meta.window_opener.set(windows.open)
    try:
        yield windows
    finally:
        meta.window_opener.reset(token)
