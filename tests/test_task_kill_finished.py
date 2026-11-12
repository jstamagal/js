"""Cancellation remains safe after a background job's private loop closes."""

import asyncio
import threading

import pytest

from js.toolkit import task_jobs


def completed_job(monkeypatch):
    closed = threading.Event()
    real_new_loop = asyncio.new_event_loop
    loops = []

    def new_loop():
        loop = real_new_loop()
        loops.append(loop)
        real_close = loop.close

        def close():
            real_close()
            closed.set()

        loop.close = close
        return loop

    monkeypatch.setattr(asyncio, "new_event_loop", new_loop)

    async def worker(index, item):
        return f"done {item}"

    job = task_jobs.start([(1, "a")], worker, lambda results: results[0],
                          agent_id="worker", depth=1, owner="test-owner")
    task_jobs.wait(job, 2)
    assert closed.wait(2)
    assert loops[0].is_closed()
    return job, loops[0]


def test_kill_finished_private_loop_preserves_result(monkeypatch):
    job, _ = completed_job(monkeypatch)
    job.kill()
    assert job.result() == "done a"
    assert not job.cancelled()


def test_finish_between_kill_check_and_closed_loop_schedule(monkeypatch):
    job, _ = completed_job(monkeypatch)
    states = iter([False, True])
    monkeypatch.setattr(job.future, "done", lambda: next(states))
    job.kill()
    assert job.result() == "done a"


def test_open_loop_runtime_error_is_not_swallowed(monkeypatch):
    job, loop = completed_job(monkeypatch)
    monkeypatch.setattr(loop, "is_closed", lambda: False)
    failure = RuntimeError("unrelated scheduling failure")

    def fail(*args):
        raise failure

    monkeypatch.setattr(loop, "call_soon_threadsafe", fail)
    with pytest.raises(RuntimeError) as caught:
        job._cancel()
    assert caught.value is failure


def test_completed_job_does_not_invoke_cancel_hook(tmp_path):
    job = task_jobs.TaskJob("finished", "worker", 1, 1, tmp_path / "result")
    called = []
    job.set_cancel(lambda: called.append(True))
    job.future.set_result("kept")
    job.kill()
    assert called == []
    assert job.result() == "kept"


def test_kill_before_registration_and_running_cancellation(tmp_path):
    early = task_jobs.TaskJob("early", "worker", 1, 1, tmp_path / "result")
    called = []
    early.kill()
    early.set_cancel(lambda: called.append(True))
    assert called == [True]
    entered = threading.Event()
    stopped = threading.Event()

    async def worker(index, item):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    job = task_jobs.start([(1, "a")], worker, lambda results: "done",
                          agent_id="worker", depth=1, owner="test-owner")
    assert entered.wait(2)
    try:
        job.kill()
        task_jobs.wait(job, 2)
        assert stopped.wait(2)
        assert job.cancelled()
    finally:
        if job.running():
            job.kill()
