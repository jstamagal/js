"""Subagent windows on the REPL screen.

Each subagent launched from the async REPL gets the next free window
(`screen.Windows`, up to `WINDOW_MAX` open). `/window new` opens one with no
subagent yet: the first line typed there launches its agent as a subagent of
the main conversation, in that window. A window shows everything it streams: its launch
and end lines, its answer, reasoning and tool exchanges. A line typed in the
window reaches the subagent: while it runs, the line is queued and delivered
as a user message at its next tool boundary (the runtime's ``steer``); once
it has finished, the line runs a new turn on the subagent's own session with
its own agent, model, effort and tools. That turn stays in the subagent's
session; the result its task returned to the parent stands, and the parent
is not told. A subagent's window that was never on screen closes when its
task ends, unless `WINDOW_KEEP_UNSEEN` is on: the parent has the answer and
the session keeps the rest.

`subagents._run_one_task_async` calls `window_opener` (set here for the REPL's
tasks) with the subagent_start fields; the `SubagentWindow` it gets back
supplies the child's telemetry, its steer, and where its prints land.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import sys
from collections.abc import Awaitable, Callable
from typing import Any

from . import display, fset, messages as msgs, runtime, screen, screen_scrollback
from .toolkit import subagents


class SubagentWindow:
    """One subagent's window and the conversation typed into it."""

    def __init__(self, owner: SubagentWindows, view: screen.View, launch: dict | None, agent: str = "") -> None:
        self._owner = owner
        self.view = view
        # What its subagent_start said of it; None until a subagent runs here.
        self.launch = dict(launch) if launch is not None else None
        # The agent the next line starts when no subagent runs here yet.
        self.agent = launch["agent"] if launch is not None else agent
        # Lines typed while a turn runs, delivered at its next tool boundary.
        self.inbox: list[str] = []
        # The child's ToolContext, for the status bar.
        self.context: Any = None
        # What runs in the window now: the subagent's task, or a turn typed after it.
        self.task: asyncio.Task | None = None
        # Runs one turn on the subagent's session; set once its task has ended.
        self._resume: Callable[[str], Awaitable[None]] | None = None

    @property
    def scrollback(self) -> screen_scrollback.Scrollback:
        return self.view.scrollback

    @property
    def number(self) -> int:
        return self.view.number

    @property
    def fresh(self) -> bool:
        """No conversation here yet: the next line starts one."""
        return self._resume is None and not self.busy()

    def telemetry(self, debug_log: Any) -> Any:
        """A Telemetry whose answer, reasoning and tool exchanges draw in this window."""

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
        elif (not self.view.seen and not fset.setting_value("WINDOW_KEEP_UNSEEN")
              and self._owner.windows.view(self.number) is self.view):
            self._owner.windows.close(self.number)

    def busy(self) -> bool:
        return self.task is not None and not self.task.done()

    def send(self, text: str) -> None:
        """A line typed in the window: queued while a turn runs, else a new
        turn, or the subagent it launches when none ran here yet."""
        if self.busy():
            self.inbox.append(text)
            with self.writing():
                msgs.say(msgs.WINDOW_QUEUED, agent=self.agent)
            return
        if self._resume is None:
            self.task = self._owner.supervisor.spawn(
                self._launch(text), kind="subagent", label=f"window {self.number}").task
            return
        self._start(text)

    async def _launch(self, text: str) -> None:
        """Run ``text`` as a task for this window's agent, from the main
        conversation, its subagent drawing here. The subagents it launches
        in turn open windows of their own."""
        adopted: list[dict] = []

        def opener(launch: dict) -> SubagentWindow | None:
            if adopted:
                return self._owner.open(launch)
            adopted.append(launch)
            self.launch = dict(launch)
            return self

        token = subagents.window_opener.set(opener)
        try:
            with self.writing():
                result = await subagents.task_async(tasks=[text], agent_id=self.agent,
                                                    context=self._owner.parent_context(), detach=False)
                if not adopted:
                    msgs.warn(msgs.FAILED, error=result)
        except Exception as exc:  # noqa: BLE001 - shown in its window like a REPL turn's
            with self.writing():
                msgs.warn(msgs.FAILED, error=f"{type(exc).__name__}: {exc}")
        finally:
            subagents.window_opener.reset(token)

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
    """The REPL screen's subagent windows: `open` is `subagents.window_opener`."""

    def __init__(self, windows: screen.Windows, supervisor: Any) -> None:
        self.windows = windows
        self.supervisor = supervisor
        self.loop: asyncio.AbstractEventLoop = supervisor.loop
        self.app: Any = None
        windows.stop = self._stop
        windows.label = self._label
        # The context a window's subagent is launched from: the main
        # conversation's, on its live settings.
        self.parent_context: Callable[[], Any] = lambda: copy.copy(runtime.T.STOCK_CONTEXT)

    def attach(self, app: Any) -> None:
        self.app = app
        self.windows.invalidate = app.invalidate

    def open(self, launch: dict) -> SubagentWindow | None:
        """A hidden window for the subagent ``launch`` describes; None when
        `WINDOW_MAX` windows are open or there is no screen."""
        if self.app is None:
            return None
        view = self.windows.open()
        if view is None:
            return None
        window = SubagentWindow(self, view, launch)
        view.owner = window
        return window

    def new(self, agent: str) -> str | None:
        """Open a window on screen, under the current one, whose first line
        launches ``agent``; why not, or None once it is open."""
        view = self.windows.open()
        if view is None:
            return msgs.WINDOW_FULL.said(max=self.windows.limit())
        view.owner = SubagentWindow(self, view, None, agent=agent)
        self.windows.show(view.number)
        return None

    def set_agent(self, number: int, agent: str) -> str | None:
        """The next line in window ``number`` starts a new conversation with
        ``agent``; why not, or None once it will."""
        view = self.windows.view(number)
        if view is None:
            return msgs.NO_SUCH_WINDOW.said(number=number)
        if view.owner is None:
            return msgs.WINDOW_AGENT_MAIN.said()
        if view.owner.busy():
            return msgs.WINDOW_AGENT_BUSY.said(number=number, agent=view.owner.agent)
        view.owner.agent, view.owner.launch, view.owner._resume = agent, None, None
        self.windows.invalidate()
        return None

    def on_screen(self) -> SubagentWindow | None:
        """The current window's subagent window; None in window 1."""
        view = self.windows.view()
        return view.owner if view is not None else None

    @staticmethod
    def _stop(view: screen.View) -> None:
        if view.owner is not None:
            view.owner.cancel()

    def _label(self, view: screen.View) -> str:
        row = self._row(view)
        mark = msgs.WINDOW_MARK_CURRENT.words() if row["current"] else ""
        return f" [{row['number']}{mark}] {' '.join(filter(None, (row['name'], row['agent'], row['state'])))}"

    def _row(self, view: screen.View) -> dict:
        owner = view.owner
        if owner is None:
            state = ""
        elif owner.busy():
            state = msgs.WINDOW_STATE_RUNNING.words()
        elif owner.fresh:
            state = msgs.WINDOW_STATE_NEW.words()
        else:
            state = msgs.WINDOW_STATE_FINISHED.words()
        return {
            "number": view.number,
            "name": view.name or (msgs.WINDOW_MAIN.words() if owner is None else ""),
            "current": view.number == self.windows.current,
            "active": view.active,
            "visible": view.number in self.windows.visible,
            "agent": owner.agent if owner is not None else "",
            "running": owner.busy() if owner is not None else False,
            "state": state,
        }

    def rows(self) -> list[dict]:
        """One row per open window, for /window list."""
        return [self._row(self.windows.view(number)) for number in self.windows.numbers()]


@contextlib.contextmanager
def opening(windows: SubagentWindows):
    """Give the subagents this context launches windows from ``windows``."""

    token = subagents.window_opener.set(windows.open)
    try:
        yield windows
    finally:
        subagents.window_opener.reset(token)
