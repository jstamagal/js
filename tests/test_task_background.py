"""task(background=true) hands back a handle at once. poll, wait and kill act
on it the way they act on a shell handle, and a run that finishes unread is
named in a js-reminder on the next user message."""

from __future__ import annotations

import asyncio
import re
import threading
import time
import types
from pathlib import Path

import pytest

from js import attach, cli, supervisor
from js.toolkit import ToolContext
from js.toolkit import meta, task_jobs


def _ctx() -> ToolContext:
    ctx = ToolContext()
    ctx.config = types.SimpleNamespace(lock_subagent_model=False, prompt_roots=None)
    return ctx


class Workers:
    """Stands in for the child turns: each blocks until released."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.cancelled = threading.Event()
        self.started = threading.Event()

    async def run(self, idx, total, item, context, parent_cfg, full_registry, agent_id, session_id, model=""):
        self.started.set()
        try:
            while not self.release.is_set():
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return f"result of {item}"


@pytest.fixture
def workers(monkeypatch):
    fake = Workers()
    monkeypatch.setattr(meta, "_run_one_task_async", fake.run)
    monkeypatch.setattr(task_jobs, "_JOBS", {})
    yield fake
    fake.release.set()


def _handle(text: str) -> str:
    match = re.search(r"HANDLE (\S+) RUNNING", text)
    assert match is not None, text
    return match.group(1)


def test_background_returns_a_handle_while_the_worker_still_runs(workers):
    began = time.monotonic()
    started = meta.task(tasks=["alpha"], agent_id="worker", background=True, context=_ctx())
    assert time.monotonic() - began < 5
    handle = _handle(started)
    assert workers.started.wait(5)
    assert task_jobs.find(handle).running()


def test_poll_reports_running_then_the_result(workers):
    handle = _handle(meta.task(tasks=["alpha"], agent_id="worker", background=True, context=_ctx()))
    polled = meta.task(action="poll", handle=handle, context=_ctx())
    assert f"HANDLE {handle} RUNNING" in polled

    workers.release.set()
    task_jobs.find(handle).future.result(timeout=5)
    done = meta.task(action="poll", handle=handle, context=_ctx())
    assert "RUNNING" not in done
    assert "result of alpha" in done


def test_wait_blocks_for_the_result_and_a_timeout_returns_running(workers):
    handle = _handle(meta.task(tasks=["alpha", "beta"], agent_id="worker", background=True, context=_ctx()))
    assert "RUNNING" in meta.task(action="wait", handle=handle, timeout=1, context=_ctx())

    threading.Timer(0.2, workers.release.set).start()
    finished = meta.task(action="wait", handle=handle, context=_ctx())
    assert "TASK_RESULTS agent=worker" in finished
    assert "1. result of alpha" in finished and "2. result of beta" in finished


def test_the_result_is_saved_where_the_handle_says(workers):
    started = meta.task(tasks=["alpha"], agent_id="worker", background=True, context=_ctx())
    path = Path(re.search(r"result is written to (\S+),", started).group(1))
    workers.release.set()
    meta.task(action="wait", handle=_handle(started), timeout=5, context=_ctx())
    assert path.read_text(encoding="utf-8") == "result of alpha"


def test_kill_cancels_the_workers(workers):
    handle = _handle(meta.task(tasks=["alpha"], agent_id="worker", background=True, context=_ctx()))
    assert workers.started.wait(5)
    killed = meta.task(action="kill", handle=handle, context=_ctx())
    assert killed.startswith(f"killed task {handle}")
    assert workers.cancelled.wait(5)
    assert not task_jobs.find(handle).running()
    assert task_jobs.completion_notes() == []


def test_handle_defaults_to_the_newest_running_task_and_unknown_is_an_error(workers):
    handle = _handle(meta.task(tasks=["alpha"], agent_id="worker", background=True, context=_ctx()))
    assert f"HANDLE {handle} RUNNING" in meta.task(action="poll", context=_ctx())
    assert meta.task(action="poll", handle="nope", context=_ctx()).startswith("ERROR: no background task")
    assert meta.task(action="dance", context=_ctx()).startswith("ERROR: unknown action")


def test_a_finished_unread_task_is_named_in_the_next_user_message(workers):
    started = meta.task(tasks=["alpha"], agent_id="worker", background=True, context=_ctx())
    handle = _handle(started)
    path = re.search(r"result is written to (\S+),", started).group(1)
    workers.release.set()
    task_jobs.find(handle).future.result(timeout=5)

    bundle = attach.UserMessageBundle({"role": "user", "content": "next"}, {"role": "user", "content": "next"})
    sent = cli._with_pending_notes({}, bundle)
    text = sent.runtime_message["content"]
    assert text.startswith("next")
    note = re.search(r"<js-reminder>(.*?)</js-reminder>", text, re.S).group(1)
    assert handle in note and path in note
    # Only once.
    again = cli._with_pending_notes({}, bundle)
    assert "<js-reminder>" not in again.runtime_message["content"]


def test_a_result_already_read_gets_no_reminder(workers):
    handle = _handle(meta.task(tasks=["alpha"], agent_id="worker", background=True, context=_ctx()))
    workers.release.set()
    meta.task(action="wait", handle=handle, timeout=5, context=_ctx())
    assert task_jobs.completion_notes() == []


def test_a_subagents_own_background_task_is_not_reminded_to_the_operator(workers):
    ctx = _ctx()
    ctx.task_depth = 1
    handle = _handle(meta.task(tasks=["alpha"], agent_id="worker", background=True, context=ctx))
    workers.release.set()
    task_jobs.find(handle).future.result(timeout=5)
    assert task_jobs.completion_notes() == []


def test_foreground_task_still_returns_the_result(workers):
    workers.release.set()
    assert meta.task(tasks=["alpha"], agent_id="worker", context=_ctx()) == "result of alpha"


def test_under_the_supervisor_the_task_outlives_the_turn_that_started_it(workers):
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    sup = supervisor.Supervisor(loop)
    supervisor.set_current(sup)
    try:
        async def turn() -> str:
            return await meta.task_async(tasks=["alpha"], agent_id="worker", background=True, context=_ctx())

        started = asyncio.run_coroutine_threadsafe(turn(), loop).result(timeout=5)
        handle = _handle(started)
        assert workers.started.wait(5)
        assert [job.kind for job in sup.jobs()] == ["subagent"]

        async def wait() -> str:
            return await meta.task_async(action="wait", handle=handle, timeout=10, context=_ctx())

        threading.Timer(0.2, workers.release.set).start()
        finished = asyncio.run_coroutine_threadsafe(wait(), loop).result(timeout=15)
        assert "result of alpha" in finished
    finally:
        supervisor.set_current(None)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)


def test_kill_under_the_supervisor_does_not_block_the_loop(workers):
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    sup = supervisor.Supervisor(loop)
    supervisor.set_current(sup)
    try:
        async def call(**kwargs) -> str:
            return await meta.task_async(context=_ctx(), **kwargs)

        started = asyncio.run_coroutine_threadsafe(
            call(tasks=["alpha"], agent_id="worker", background=True), loop).result(timeout=5)
        assert workers.started.wait(5)
        began = time.monotonic()
        killed = asyncio.run_coroutine_threadsafe(
            call(action="kill", handle=_handle(started)), loop).result(timeout=10)
        assert killed.startswith("killed task")
        assert time.monotonic() - began < task_jobs.KILL_GRACE_S
        assert workers.cancelled.wait(5)
    finally:
        supervisor.set_current(None)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
