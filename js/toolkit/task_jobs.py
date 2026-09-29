"""Background `task` runs: a handle the model polls, waits on, or kills, and a
note for the next user message when one finishes without the model having
read its result.

A background run is the same fan-out a foreground `task` call runs. Under the
REPL supervisor it is one "subagent" job on the REPL's loop, so it outlives the
turn that started it and shows in /jobs. Without a supervisor (`-p`, tests) it
runs on a private loop in a daemon thread, and ends with the process.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import os
import secrets
import threading
import time
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from .. import paths

# Finished jobs kept for a later poll once their result was delivered or noted.
KEEP_FINISHED_JOBS = 5


class TaskJob:
    def __init__(self, job_id: str, agent_id: str, count: int, depth: int, result_path: Path) -> None:
        self.id = job_id
        self.agent_id = agent_id
        self.count = count
        self.depth = depth
        self.result_path = result_path
        self.started = time.monotonic()
        self.finished_at: float | None = None
        self.future: concurrent.futures.Future[str] = concurrent.futures.Future()
        self.workers_done = 0
        self.delivered = False   # the model has the result (poll, wait, kill)
        self.noted = False       # a completion note went out
        self.saved = False       # result_path holds the result
        self._cancel: Callable[[], None] | None = None
        self._kill_requested = False
        self._lock = threading.Lock()

    def running(self) -> bool:
        return not self.future.done()

    def elapsed(self) -> float:
        return (self.finished_at or time.monotonic()) - self.started

    def cancelled(self) -> bool:
        return self.future.cancelled()

    def result(self) -> str:
        return self.future.result()

    def set_cancel(self, cancel: Callable[[], None]) -> None:
        with self._lock:
            self._cancel = cancel
            kill = self._kill_requested
        if kill:
            cancel()

    def kill(self) -> None:
        with self._lock:
            self._kill_requested = True
            cancel = self._cancel
        if cancel is not None:
            cancel()


_JOBS: dict[str, TaskJob] = {}
_JOBS_LOCK = threading.Lock()
_SEQ = 0


def _next_id() -> str:
    global _SEQ
    with _JOBS_LOCK:
        _SEQ += 1
        return f"t{_SEQ}"


def _prune() -> None:
    with _JOBS_LOCK:
        settled = [job for job in _JOBS.values()
                   if not job.running() and (job.delivered or job.noted)]
        for stale in settled[:-KEEP_FINISHED_JOBS] if len(settled) > KEEP_FINISHED_JOBS else []:
            _JOBS.pop(stale.id, None)


def find(handle: str | None) -> TaskJob | None:
    """The job named ``handle``; without one, the newest running job, else the
    newest job."""
    with _JOBS_LOCK:
        if handle:
            return _JOBS.get(str(handle).strip())
        running = [job for job in _JOBS.values() if job.running()]
        if running:
            return running[-1]
        return next(reversed(_JOBS.values()), None) if _JOBS else None


def _save(job: TaskJob, text: str) -> None:
    try:
        job.result_path.parent.mkdir(parents=True, exist_ok=True)
        partial = job.result_path.with_name(f".{job.result_path.name}.tmp")
        partial.write_text(text, encoding="utf-8")
        os.replace(partial, job.result_path)
        job.saved = True
    except OSError:
        job.saved = False


def _settle(job: TaskJob, outcome: concurrent.futures.Future | asyncio.Future,
            on_done: Callable[[], None] | None) -> None:
    """Copy a finished run's outcome onto ``job.future``, saving a result."""
    if job.future.done():
        return
    job.finished_at = time.monotonic()
    if outcome.cancelled():
        job.future.cancel()
    else:
        error = outcome.exception()
        text = f"ERROR {type(error).__name__}: {error}" if error is not None else str(outcome.result())
        _save(job, text)
        job.future.set_result(text)
    if on_done is not None:
        with contextlib.suppress(Exception):
            on_done()
    _prune()


def start(
    indexed_items: list[tuple[int, Any]],
    coro_factory: Callable[[int, Any], Coroutine[Any, Any, str]],
    assemble: Callable[[list[str | None]], str],
    *,
    agent_id: str,
    depth: int,
    on_done: Callable[[], None] | None = None,
    on_loop: bool = False,
) -> TaskJob:
    """Start the fan-out in the background and return its job at once.

    ``on_loop`` means the caller is a coroutine on the supervisor's loop;
    otherwise the caller is a thread off any loop.
    """
    from ..supervisor import get_current

    job_id = _next_id()
    result_path = paths.tool_results_dir() / f"task-{job_id}-{secrets.token_hex(4)}.txt"
    job = TaskJob(job_id, agent_id, len(indexed_items), depth, result_path)

    async def run_all() -> str:
        results: list[str | None] = [None] * len(indexed_items)

        async def one(idx: int, item: Any) -> None:
            try:
                results[idx - 1] = await coro_factory(idx, item)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one worker's failure is its result
                results[idx - 1] = f"ERROR {type(exc).__name__}: {exc}"
            job.workers_done += 1

        await asyncio.gather(*(one(idx, item) for idx, item in indexed_items))
        return assemble(results)

    with _JOBS_LOCK:
        _JOBS[job.id] = job
    label = f"task {job.id} (background)"
    sup = get_current()
    if sup is not None and on_loop:
        tracked = sup.spawn(run_all(), kind="subagent", label=label)
        tracked.task.add_done_callback(lambda done: _settle(job, done, on_done))
        job.set_cancel(lambda: sup.loop.call_soon_threadsafe(tracked.task.cancel))
    elif sup is not None:
        outer = sup.spawn_from_thread(run_all(), kind="subagent", label=label)
        outer.add_done_callback(lambda done: _settle(job, done, on_done))
        job.set_cancel(outer.cancel)
    else:
        def runner() -> None:
            loop = asyncio.new_event_loop()
            try:
                task = loop.create_task(run_all())
                task.add_done_callback(lambda done: _settle(job, done, on_done))
                job.set_cancel(lambda: loop.call_soon_threadsafe(task.cancel))
                with contextlib.suppress(BaseException):
                    loop.run_until_complete(task)
            finally:
                loop.close()

        threading.Thread(target=runner, daemon=True, name=f"js-task-{job.id}").start()
    return job


def started_text(job: TaskJob) -> str:
    plural = "task" if job.count == 1 else "tasks"
    return "\n".join([
        f"task running in the background (handle {job.id}): {job.count} {plural} for agent "
        f"`{job.agent_id}`. It keeps running while you work.",
        f"Poll it with action=\"poll\", handle=\"{job.id}\", action=\"wait\", handle=\"{job.id}\", "
        f"timeout=N to block for it, or action=\"kill\", handle=\"{job.id}\" to stop it.",
        f"When it finishes its result is written to {job.result_path}, and the next user "
        "message says so unless you have read it by then.",
        f"HANDLE {job.id} RUNNING",
    ])


# How long a kill waits for the cancelled workers to wind down.
KILL_GRACE_S = 5.0


def report(job: TaskJob, action: str, *, was_running: bool = False) -> str:
    """The answer to poll, wait or kill once any waiting is over.
    ``was_running`` says whether a killed job was still running when the kill
    was asked for."""
    if action == "kill" and was_running and (job.running() or job.cancelled()):
        job.delivered = True
        return f"killed task {job.id} after {job.elapsed():.0f}s"
    if job.running():
        return "\n".join([
            f"task {job.id} still running after {job.elapsed():.0f}s "
            f"({job.workers_done} of {job.count} done).",
            f"HANDLE {job.id} RUNNING",
        ])
    job.delivered = True
    if job.cancelled():
        return f"task {job.id} was cancelled after {job.elapsed():.0f}s; it has no result"
    where = f"; the result is also at {job.result_path}" if job.saved else ""
    prefix = f"task {job.id} had already finished" if action == "kill" else f"task {job.id} finished"
    return f"{prefix} after {job.elapsed():.0f}s{where}\n{job.result()}"


def wait(job: TaskJob, timeout: float | None) -> None:
    """Block up to ``timeout`` seconds for ``job`` to end. For a caller off
    the job's loop."""
    with contextlib.suppress(TimeoutError, concurrent.futures.CancelledError):
        job.future.result(timeout=timeout)


async def wait_async(job: TaskJob, timeout: float | None) -> None:
    """Await up to ``timeout`` seconds for ``job`` to end without blocking
    the loop. Cancelling the caller cancels the wait, not the job."""
    try:
        await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(job.future)), timeout)
    except TimeoutError:
        pass
    except asyncio.CancelledError:
        if not job.future.cancelled():
            raise


def completion_notes() -> list[str]:
    """One reminder per job the main agent started that has finished since
    the last call and whose result the model has not read."""
    notes: list[str] = []
    with _JOBS_LOCK:
        jobs = list(_JOBS.values())
    for job in jobs:
        if job.depth != 0 or job.running() or job.delivered or job.noted:
            continue
        job.noted = True
        if job.cancelled():
            notes.append(f"<js-reminder>Background task {job.id} (agent `{job.agent_id}`) was "
                         f"cancelled after {job.elapsed():.0f}s and has no result.</js-reminder>")
            continue
        where = (f"Its result is at {job.result_path}" if job.saved
                 else "Its result could not be saved to a file")
        notes.append(
            f"<js-reminder>Background task {job.id} (agent `{job.agent_id}`, {job.count} "
            f"{'task' if job.count == 1 else 'tasks'}) finished after {job.elapsed():.0f}s. "
            f"{where}; read it, or call task with action=\"poll\", handle=\"{job.id}\".</js-reminder>"
        )
    _prune()
    return notes
