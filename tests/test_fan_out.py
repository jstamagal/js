"""The task tool's fan-out has two ramps. Existing subagent tests cover the
no-supervisor ramp (asyncio.run gather). This covers the supervisor ramp: when
a non-blocking REPL loop is live, subagents schedule onto it as cancelable jobs
via run_coroutine_threadsafe, called from an off-loop (tool-dispatch) thread."""

from __future__ import annotations

import asyncio
import threading
import types

from js import supervisor
from js.toolkit import ToolContext, subagents


def _loop_in_thread() -> tuple[asyncio.AbstractEventLoop, threading.Thread]:
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, name="js-repl-loop", daemon=True)
    t.start()
    return loop, t


def _task(tasks: list[str], worker) -> list[str]:
    """The numbered results of the task tool running ``worker(idx, item)`` as
    each subagent, called off the loop as a tool dispatch thread calls it."""
    async def subagent(idx, total, item, *args, **kwargs):
        return await worker(idx, item)

    context = ToolContext()
    context.config = types.SimpleNamespace(lock_subagent_model=False, prompt_roots=None)
    context.subagent_assembly = subagents.SubagentAssembly(worker=subagent)
    return subagents.task(tasks=tasks, agent_id="worker", context=context).split("\n\n")[1:]


def test_supervisor_ramp_runs_subagents_as_jobs_off_loop():
    loop, thread = _loop_in_thread()
    sup = supervisor.Supervisor(loop)
    supervisor.set_current(sup)
    seen_kinds: list[str] = []
    try:
        async def work(idx, item):
            # Snapshot our job kind from the registry while we run.
            seen_kinds.extend(j.kind for j in sup.jobs())
            await asyncio.sleep(0.01)
            return item.upper()

        # Called from the MAIN thread — off the loop, exactly like a tool
        # dispatch worker calling task().
        results = _task(["a", "b", "c"], work)
        assert results == ["1. A", "2. B", "3. C"]
        assert seen_kinds and set(seen_kinds) == {"subagent"}
    finally:
        supervisor.set_current(None)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)


def test_supervisor_ramp_reports_per_task_errors():
    loop, thread = _loop_in_thread()
    sup = supervisor.Supervisor(loop)
    supervisor.set_current(sup)
    try:
        async def work(idx, item):
            if idx == 2:
                raise RuntimeError("boom")
            return "ok"

        results = _task(["a", "b"], work)
        assert results[0] == "1. ok"
        assert results[1].startswith("2. ERROR RuntimeError: boom")
    finally:
        supervisor.set_current(None)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
