"""task(background=true) hands back a handle at once. poll, wait and kill act
on it the way they act on a shell handle, and a run that finishes unread is
named in a js-reminder on the next user message."""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
import types

import pytest

from js import attach, cli, runtime, supervisor
from js.toolkit import ToolContext
from js.toolkit import meta, task_jobs
from js.toolkit.registry import ToolRegistry


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


@pytest.fixture
def ctx(monkeypatch):
    """The main agent's context, as the REPL's turns use it."""
    main = _ctx()
    monkeypatch.setattr(runtime.T, "STOCK_CONTEXT", main)
    return main


@pytest.fixture
def loop_sup():
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    sup = supervisor.Supervisor(loop)
    supervisor.set_current(sup)
    try:
        yield loop, sup
    finally:
        supervisor.set_current(None)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)


def _handle(text: str) -> str:
    match = re.search(r"HANDLE (\S+) RUNNING", text)
    assert match is not None, text
    return match.group(1)

def _job(handle: str, ctx: ToolContext) -> task_jobs.TaskJob:
    job = task_jobs.find(handle, ctx.task_owner)
    assert job is not None
    return job


def _notes(ctx: ToolContext) -> list[str]:
    return task_jobs.completion_notes(ctx.task_owner)


def _bundle() -> attach.UserMessageBundle:
    return attach.UserMessageBundle({"role": "user", "content": "next"}, {"role": "user", "content": "next"})


def _subagent_ctx() -> ToolContext:
    child = _ctx()
    child.task_depth = 1
    return child


def test_background_returns_a_handle_while_the_worker_still_runs(workers, ctx):
    began = time.monotonic()
    started = meta.task(tasks=["alpha"], agent_id="worker", background=True, context=ctx)
    assert time.monotonic() - began < 5
    handle = _handle(started)
    assert workers.started.wait(5)
    assert _job(handle, ctx).running()


def test_poll_reports_running_then_the_result(workers, ctx):
    handle = _handle(meta.task(tasks=["alpha"], agent_id="worker", background=True, context=ctx))
    polled = meta.task(action="poll", handle=handle, context=ctx)
    assert f"HANDLE {handle} RUNNING" in polled

    workers.release.set()
    _job(handle, ctx).future.result(timeout=5)
    done = meta.task(action="poll", handle=handle, context=ctx)
    assert "RUNNING" not in done
    assert "result of alpha" in done


def test_wait_blocks_for_the_result_and_a_timeout_returns_running(workers, ctx):
    handle = _handle(meta.task(tasks=["alpha", "beta"], agent_id="worker", background=True, context=ctx))
    assert "RUNNING" in meta.task(action="wait", handle=handle, timeout=1, context=ctx)

    threading.Timer(0.2, workers.release.set).start()
    finished = meta.task(action="wait", handle=handle, context=ctx)
    assert "TASK_RESULTS agent=worker" in finished
    assert "1. result of alpha" in finished and "2. result of beta" in finished


def test_the_result_is_saved_where_the_handle_says(workers, ctx):
    started = meta.task(tasks=["alpha"], agent_id="worker", background=True, context=ctx)
    job = _job(_handle(started), ctx)
    assert str(job.result_path) in started
    workers.release.set()
    meta.task(action="wait", handle=job.id, timeout=5, context=ctx)
    assert job.result_path.read_text(encoding="utf-8") == "result of alpha"


def test_kill_cancels_the_workers(workers, ctx):
    handle = _handle(meta.task(tasks=["alpha"], agent_id="worker", background=True, context=ctx))
    assert workers.started.wait(5)
    killed = meta.task(action="kill", handle=handle, context=ctx)
    assert handle in killed and "RUNNING" not in killed
    assert workers.cancelled.wait(5)
    assert _job(handle, ctx).cancelled()
    assert _notes(ctx) == []


def test_handle_defaults_to_the_newest_running_task_and_unknown_is_an_error(workers, ctx):
    handle = _handle(meta.task(tasks=["alpha"], agent_id="worker", background=True, context=ctx))
    assert f"HANDLE {handle} RUNNING" in meta.task(action="poll", context=ctx)
    assert meta.task(action="poll", handle="nope", context=ctx).startswith("ERROR")
    assert meta.task(action="dance", context=ctx).startswith("ERROR")


def test_a_finished_unread_task_is_named_in_the_next_user_message(workers, ctx):
    started = meta.task(tasks=["alpha"], agent_id="worker", background=True, context=ctx)
    job = _job(_handle(started), ctx)
    workers.release.set()
    job.future.result(timeout=5)

    sent = cli._with_pending_notes({}, _bundle())
    text = sent.runtime_message["content"]
    assert text.startswith("next")
    note = re.search(r"<js-reminder>(.*?)</js-reminder>", text, re.S).group(1)
    assert job.id in note and str(job.result_path) in note
    # Only once.
    again = cli._with_pending_notes({}, _bundle())
    assert "<js-reminder>" not in again.runtime_message["content"]


def test_a_result_already_read_gets_no_reminder(workers, ctx):
    handle = _handle(meta.task(tasks=["alpha"], agent_id="worker", background=True, context=ctx))
    workers.release.set()
    meta.task(action="wait", handle=handle, timeout=5, context=ctx)
    assert _notes(ctx) == []


def test_a_subagents_own_background_task_is_not_reminded_to_the_operator(workers, ctx):
    child = _subagent_ctx()
    handle = _handle(meta.task(tasks=["alpha"], agent_id="worker", background=True, context=child))
    workers.release.set()
    _job(handle, child).future.result(timeout=5)
    assert _notes(ctx) == []
    assert _notes(child) == []


def test_each_context_sees_only_its_own_tasks(workers, ctx):
    handle = _handle(meta.task(tasks=["alpha"], agent_id="worker", background=True, context=ctx))
    child = _subagent_ctx()
    # The subagent cannot reach the main agent's task, by handle or by default.
    assert meta.task(action="poll", handle=handle, context=child).startswith("ERROR")
    assert meta.task(action="poll", context=child).startswith("ERROR")
    assert meta.task(action="kill", handle=handle, context=child).startswith("ERROR")
    own = _handle(meta.task(tasks=["beta"], agent_id="worker", background=True, context=child))
    # Without a handle the main agent gets its own task, not the newer one.
    assert f"HANDLE {handle} RUNNING" in meta.task(action="poll", context=ctx)
    assert task_jobs.find(own, ctx.task_owner) is None

    workers.release.set()
    _job(handle, ctx).future.result(timeout=5)
    _job(own, child).future.result(timeout=5)
    notes = _notes(ctx)
    assert len(notes) == 1 and handle in notes[0]


def test_reset_drops_the_reminder_for_tasks_of_the_old_conversation(workers, ctx, tmp_path):
    handle = _handle(meta.task(tasks=["alpha"], agent_id="worker", background=True, context=ctx))
    workers.release.set()
    _job(handle, ctx).future.result(timeout=5)
    cfg = types.SimpleNamespace(session_file=tmp_path / "session.jsonl")
    cli._cmd_reset("", {"messages": []}, cfg)
    assert "<js-reminder>" not in cli._with_pending_notes({}, _bundle()).runtime_message["content"]


def test_finished_subagent_tasks_do_not_pile_up(workers, ctx):
    workers.release.set()
    child = _subagent_ctx()
    for _ in range(task_jobs.KEEP_FINISHED_JOBS + 4):
        handle = _handle(meta.task(tasks=["alpha"], agent_id="worker", background=True, context=child))
        _job(handle, child).future.result(timeout=5)
    time.sleep(0.05)
    assert len(task_jobs._JOBS) <= task_jobs.KEEP_FINISHED_JOBS + 1


def test_foreground_task_still_returns_the_result(workers, ctx):
    workers.release.set()
    assert meta.task(tasks=["alpha"], agent_id="worker", context=ctx) == "result of alpha"


def test_under_the_supervisor_the_task_outlives_the_turn_that_started_it(workers, ctx, loop_sup):
    loop, sup = loop_sup

    async def turn() -> str:
        return await meta.task_async(tasks=["alpha"], agent_id="worker", background=True, context=ctx)

    started = asyncio.run_coroutine_threadsafe(turn(), loop).result(timeout=5)
    handle = _handle(started)
    assert workers.started.wait(5)
    assert [job.kind for job in sup.jobs()] == ["subagent"]

    async def wait() -> str:
        return await meta.task_async(action="wait", handle=handle, timeout=10, context=ctx)

    threading.Timer(0.2, workers.release.set).start()
    finished = asyncio.run_coroutine_threadsafe(wait(), loop).result(timeout=15)
    assert "result of alpha" in finished


def test_a_sync_call_under_the_supervisor_runs_the_task_on_the_repl_loop(workers, ctx, loop_sup):
    _loop, sup = loop_sup
    # A dispatch thread calls the sync tool while the REPL loop is live.
    started = meta.task(tasks=["alpha"], agent_id="worker", background=True, context=ctx)
    handle = _handle(started)
    assert workers.started.wait(5)
    deadline = time.monotonic() + 5
    while not sup.jobs() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert [job.kind for job in sup.jobs()] == ["subagent"]
    assert "RUNNING" in meta.task(action="poll", handle=handle, context=ctx)
    threading.Timer(0.2, workers.release.set).start()
    assert "result of alpha" in meta.task(action="wait", handle=handle, timeout=10, context=ctx)


def test_kill_of_a_sync_started_task_under_the_supervisor_cancels_it(workers, ctx, loop_sup):
    handle = _handle(meta.task(tasks=["alpha"], agent_id="worker", background=True, context=ctx))
    assert workers.started.wait(5)
    meta.task(action="kill", handle=handle, context=ctx)
    assert workers.cancelled.wait(5)
    assert _job(handle, ctx).cancelled()


def test_the_runtime_dispatch_passes_background_and_handle_through(workers, ctx, loop_sup):
    loop, _sup = loop_sup
    registry = ToolRegistry(meta.tools(), {})

    def dispatch(call_id: str, args: str) -> str:
        pc = runtime._PendingToolCall(call_id, "task", [args])

        async def go():
            return await runtime._dispatch_fan_out_async(
                pc, runtime.Telemetry(None), 256 * 1024, False,
                runtime.ToolErrorTracker(), registry, ctx)

        return asyncio.run_coroutine_threadsafe(go(), loop).result(timeout=10)[2]

    started = dispatch("c1", '{"tasks": ["alpha"], "agent_id": "worker", "background": true}')
    handle = _handle(started)
    assert workers.started.wait(5)
    assert f"HANDLE {handle} RUNNING" in dispatch("c2", json.dumps({"action": "poll", "handle": handle}))
    threading.Timer(0.2, workers.release.set).start()
    finished = dispatch("c3", json.dumps({"action": "wait", "handle": handle, "timeout": 10}))
    assert "result of alpha" in finished


def test_kill_under_the_supervisor_does_not_block_the_loop(workers, ctx, loop_sup):
    loop, _sup = loop_sup

    async def call(**kwargs) -> str:
        return await meta.task_async(context=ctx, **kwargs)

    started = asyncio.run_coroutine_threadsafe(
        call(tasks=["alpha"], agent_id="worker", background=True), loop).result(timeout=5)
    assert workers.started.wait(5)
    began = time.monotonic()
    handle = _handle(started)
    killed = asyncio.run_coroutine_threadsafe(call(action="kill", handle=handle), loop).result(timeout=10)
    assert handle in killed and "RUNNING" not in killed
    assert time.monotonic() - began < task_jobs.KILL_GRACE_S
    assert workers.cancelled.wait(5)
