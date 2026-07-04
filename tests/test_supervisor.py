"""Supervisor registry semantics: spawn tracks, done deregisters, cancel stops,
spawn_from_thread schedules across the thread boundary. Pure — stub coroutines,
no runtime."""

from __future__ import annotations

import asyncio
import threading

import pytest

from js.supervisor import Supervisor, get_current, set_current


def test_spawn_tracks_then_deregisters_on_completion():
    async def drive():
        sup = Supervisor(asyncio.get_running_loop())

        async def work():
            await asyncio.sleep(0.01)
            return "done"

        job = sup.spawn(work(), kind="turn", label="t1")
        assert sup.jobs("turn") == [job]
        assert sup.turn_active() is True
        result = await job.task
        assert result == "done"
        # done-callback fires as a callback, not inline — yield once so it runs.
        await asyncio.sleep(0)
        assert sup.jobs() == []
        assert sup.turn_active() is False

    asyncio.run(drive())


def test_cancel_stops_a_running_job_and_preserves_partial():
    async def drive():
        sup = Supervisor(asyncio.get_running_loop())
        marks: list[str] = []

        async def work():
            marks.append("started")
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                marks.append("cancelled")
                raise

        job = sup.spawn(work(), kind="turn")
        await asyncio.sleep(0.01)          # let it start
        assert sup.cancel(job.id) is True
        with pytest.raises(asyncio.CancelledError):
            await job.task
        assert marks == ["started", "cancelled"]
        assert sup.cancel(job.id) is False  # already done → no-op

    asyncio.run(drive())


def test_cancel_kind_cancels_all_of_a_kind_only():
    async def drive():
        sup = Supervisor(asyncio.get_running_loop())

        async def sleeper():
            await asyncio.sleep(10)

        turn = sup.spawn(sleeper(), kind="turn")
        subs = [sup.spawn(sleeper(), kind="subagent") for _ in range(2)]
        assert sup.cancel_kind("subagent") == 2
        # Let the cancelled tasks unwind so their done-callbacks deregister them.
        await asyncio.gather(*(s.task for s in subs), return_exceptions=True)
        assert [j.kind for j in sup.jobs()] == ["turn"]
        sup.cancel(turn.id)
        await asyncio.gather(turn.task, return_exceptions=True)

    asyncio.run(drive())


def test_spawn_from_thread_schedules_onto_the_loop():
    async def drive():
        loop = asyncio.get_running_loop()
        sup = Supervisor(loop)
        seen: list[str] = []

        async def work():
            seen.append(f"ran on {threading.current_thread().name}")
            return 42

        # Call spawn_from_thread from a genuinely different thread while the loop
        # runs, exactly as a tool-dispatch executor thread would.
        fut_box: list = []

        def off_loop():
            fut_box.append(sup.spawn_from_thread(work(), kind="subagent", label="s1"))

        t = threading.Thread(target=off_loop, name="js-dispatch")
        t.start()
        # Drive the loop until the worker thread has handed us its concurrent future.
        while not fut_box:
            await asyncio.sleep(0.001)
        result = await asyncio.wrap_future(fut_box[0])
        t.join()
        assert result == 42
        assert seen and seen[0].startswith("ran on ")

    asyncio.run(drive())


def test_get_set_current_roundtrip():
    assert get_current() is None
    sentinel = object()
    set_current(sentinel)  # type: ignore[arg-type]
    try:
        assert get_current() is sentinel
    finally:
        set_current(None)
    assert get_current() is None
