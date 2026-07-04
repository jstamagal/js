"""The supervisor: one registry of running jobs on one event loop.

A `Job` wraps an `asyncio.Task` — a main turn or a subagent turn — with an id,
a kind, and a label. The REPL owns the loop; `spawn` schedules a coroutine as a
tracked task, `spawn_from_thread` does the same from a tool-dispatch executor
thread (via `run_coroutine_threadsafe`), and `cancel` / `cancel_kind` stop jobs
by id or category. This is the thing that makes "fire 10 subagents while a turn
runs and still type" real: every agent is an addressable job in one place.

Leaf module — no imports from cli/runtime. See docs/nonblocking-windows.md.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import itertools
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any


@dataclass
class Job:
    """One tracked coroutine. `task` is the live `asyncio.Task`; `kind` is
    "turn" (a main REPL turn) or "subagent" (a fan-out worker)."""

    id: int
    kind: str
    label: str
    task: asyncio.Task


class Supervisor:
    """Registry of running jobs on a single event loop. Not thread-safe by
    design: mutation happens on the loop thread (spawn/cancel/done-callback);
    `spawn_from_thread` is the one method safe to call off-loop."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self._jobs: dict[int, Job] = {}
        self._ids = itertools.count(1)

    def spawn(self, coro: Coroutine[Any, Any, Any], *, kind: str, label: str = "") -> Job:
        """Schedule `coro` as a tracked task on the loop. Must be called from the
        loop thread. The job deregisters itself when the task finishes."""
        job = Job(id=next(self._ids), kind=kind, label=label, task=self.loop.create_task(coro))
        self._jobs[job.id] = job
        job.task.add_done_callback(lambda _t, _id=job.id: self._jobs.pop(_id, None))
        return job

    def spawn_from_thread(
        self, coro: Coroutine[Any, Any, Any], *, kind: str, label: str = ""
    ) -> concurrent.futures.Future:
        """Schedule `coro` from OFF the loop thread (a tool-dispatch executor
        thread). Returns a `concurrent.futures.Future`; block on `.result()` to
        wait. Registration happens on the loop via `spawn`."""
        return asyncio.run_coroutine_threadsafe(
            self._spawn_and_await(coro, kind=kind, label=label), self.loop
        )

    async def _spawn_and_await(
        self, coro: Coroutine[Any, Any, Any], *, kind: str, label: str
    ) -> Any:
        job = self.spawn(coro, kind=kind, label=label)
        return await job.task

    def cancel(self, job_id: int) -> bool:
        """Cancel one job by id. Returns True if a live job was cancelled."""
        job = self._jobs.get(job_id)
        if job is None or job.task.done():
            return False
        job.task.cancel()
        return True

    def cancel_kind(self, kind: str) -> int:
        """Cancel every live job of `kind`. Returns the count cancelled."""
        return sum(self.cancel(job.id) for job in self.jobs(kind))

    def jobs(self, kind: str | None = None) -> list[Job]:
        """Snapshot of live jobs, optionally filtered by kind, id-ordered."""
        return [j for j in sorted(self._jobs.values(), key=lambda j: j.id)
                if kind is None or j.kind == kind]

    def turn_active(self) -> bool:
        """True if a main turn is currently running (used to queue new input)."""
        return any(not j.task.done() for j in self.jobs("turn"))


_current: Supervisor | None = None


def get_current() -> Supervisor | None:
    """The active supervisor, or None outside the non-blocking REPL (one-shot
    `-p`, bench, tests). `task()` uses this to decide its fan-out ramp."""
    return _current


def set_current(sup: Supervisor | None) -> None:
    global _current
    _current = sup
