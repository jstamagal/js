import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from js.toolkit import ToolContext, meta, task_jobs


@pytest.mark.parametrize(("value", "expected"), [(None, None), (0, 0.0), (-2, 0.0), (2, 2.0)])
def test_wait_limit_only_omitted_is_indefinite(value, expected):
    assert meta._wait_limit(value) == expected


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("timeout", [0, -1])
def test_zero_wait_returns_live_background_handle(monkeypatch, asynchronous, timeout):
    monkeypatch.setattr(task_jobs, "_JOBS", {})
    context = ToolContext()
    release = threading.Event()
    started = threading.Event()

    async def worker(index, item):
        started.set()
        await asyncio.to_thread(release.wait)
        return "completed worker"

    job = task_jobs.start([(1, "work")], worker, lambda results: results[0],
                          agent_id="worker", depth=0, owner=context.task_owner,
                          on_loop=False)
    assert started.wait(2)

    def wait_once():
        args = {"action": "wait", "handle": job.id, "timeout": timeout, "context": context}
        return asyncio.run(meta.task_async(**args)) if asynchronous else meta.task(**args)

    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(wait_once)
        try:
            report = result.result(timeout=2)
            assert job.running()
            assert job.id in report
        finally:
            release.set()
            task_jobs.wait(job, 2)
    assert job.future.result(timeout=2) == "completed worker"
