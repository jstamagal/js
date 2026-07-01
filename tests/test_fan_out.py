"""meta._fan_out has two ramps. Existing subagent tests cover the no-supervisor
ramp (asyncio.run gather). This covers the supervisor ramp: when a non-blocking
REPL loop is live, subagents schedule onto it as cancelable jobs via
run_coroutine_threadsafe, called from an off-loop (tool-dispatch) thread."""

from __future__ import annotations

import asyncio
import threading

from js import supervisor
from js.toolkit.meta import _fan_out


def _loop_in_thread() -> tuple[asyncio.AbstractEventLoop, threading.Thread]:
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, name="js-repl-loop", daemon=True)
    t.start()
    return loop, t


def test_supervisor_ramp_runs_subagents_as_jobs_off_loop():
    loop, thread = _loop_in_thread()
    sup = supervisor.Supervisor(loop)
    supervisor.set_current(sup)
    seen_kinds: list[str] = []
    try:
        def factory(idx, item):
            async def work():
                # Snapshot our job kind from the registry while we run.
                seen_kinds.extend(j.kind for j in sup.jobs())
                await asyncio.sleep(0.01)
                return f"{idx}. {item.upper()}"
            return work()

        # Called from the MAIN thread — off the loop, exactly like a tool
        # dispatch worker calling task().
        results = _fan_out([(1, "a"), (2, "b"), (3, "c")], factory)
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
        def factory(idx, item):
            async def work():
                if idx == 2:
                    raise RuntimeError("boom")
                return f"{idx}. ok"
            return work()

        results = _fan_out([(1, "a"), (2, "b")], factory)
        assert results[0] == "1. ok"
        assert results[1].startswith("2. ERROR RuntimeError: boom")
    finally:
        supervisor.set_current(None)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
