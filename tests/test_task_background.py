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
from repl_driver import record_turns, run_async as run_repl
from test_runtime_offline_integration import offline_config
from turn_driver import run_async as run_turn_async

from js import (
    runtime,
    supervisor,
)
from js.toolkit import ToolContext, subagents, task_jobs
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
    monkeypatch.setattr(subagents, "_run_one_task_async", fake.run)
    monkeypatch.setattr(task_jobs, "_JOBS", {})
    yield fake
    fake.release.set()


@pytest.fixture
def ctx(monkeypatch):
    """The main agent's context, as the REPL's turns use it."""
    main = _ctx()
    main.delivers_task_results = True
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


def _sent(monkeypatch, tmp_path, *lines: str) -> list[str]:
    """The user message each prompt among `lines` starts its turn with, typed
    into a REPL session."""
    cfg = offline_config(tmp_path)
    cfg.prompts_dir.mkdir(parents=True, exist_ok=True)
    (cfg.prompts_dir / "01-prompt.md").write_text("sys\n", encoding="utf-8")
    turns = record_turns(monkeypatch)
    run_repl(monkeypatch, cfg, list(lines), wait=True)
    return [str(turn.messages[-1]["content"]) for turn in turns]


def _subagent_ctx() -> ToolContext:
    child = _ctx()
    child.delivers_task_results = True
    child.task_depth = 1
    return child


def test_background_returns_a_handle_while_the_worker_still_runs(workers, ctx):
    began = time.monotonic()
    started = subagents.task(tasks=["alpha"], agent_id="worker", background=True, context=ctx)
    assert time.monotonic() - began < 5
    handle = _handle(started)
    assert workers.started.wait(5)
    assert _job(handle, ctx).running()


def test_poll_reports_running_then_the_result(workers, ctx):
    handle = _handle(subagents.task(tasks=["alpha"], agent_id="worker", background=True, context=ctx))
    polled = subagents.task(action="poll", handle=handle, context=ctx)
    assert f"HANDLE {handle} RUNNING" in polled

    workers.release.set()
    _job(handle, ctx).future.result(timeout=5)
    done = subagents.task(action="poll", handle=handle, context=ctx)
    assert "RUNNING" not in done
    assert "result of alpha" in done


def test_wait_blocks_for_the_result_and_a_timeout_returns_running(workers, ctx):
    handle = _handle(subagents.task(tasks=["alpha", "beta"], agent_id="worker", background=True, context=ctx))
    assert "RUNNING" in subagents.task(action="wait", handle=handle, timeout=1, context=ctx)

    threading.Timer(0.2, workers.release.set).start()
    finished = subagents.task(action="wait", handle=handle, context=ctx)
    assert "TASK_RESULTS agent=worker" in finished
    assert "1. result of alpha" in finished and "2. result of beta" in finished


def test_the_result_is_saved_where_the_handle_says(workers, ctx):
    started = subagents.task(tasks=["alpha"], agent_id="worker", background=True, context=ctx)
    job = _job(_handle(started), ctx)
    assert str(job.result_path) in started
    workers.release.set()
    subagents.task(action="wait", handle=job.id, timeout=5, context=ctx)
    assert job.result_path.read_text(encoding="utf-8") == "result of alpha"


def test_kill_cancels_the_workers(workers, ctx):
    handle = _handle(subagents.task(tasks=["alpha"], agent_id="worker", background=True, context=ctx))
    assert workers.started.wait(5)
    killed = subagents.task(action="kill", handle=handle, context=ctx)
    assert handle in killed and "RUNNING" not in killed
    assert workers.cancelled.wait(5)
    assert _job(handle, ctx).cancelled()
    assert _notes(ctx) == []


def test_handle_defaults_to_the_newest_running_task_and_unknown_is_an_error(workers, ctx):
    handle = _handle(subagents.task(tasks=["alpha"], agent_id="worker", background=True, context=ctx))
    assert f"HANDLE {handle} RUNNING" in subagents.task(action="poll", context=ctx)
    assert subagents.task(action="poll", handle="nope", context=ctx).startswith("ERROR")
    assert subagents.task(action="dance", context=ctx).startswith("ERROR")


def test_a_finished_unread_task_is_named_in_the_next_user_message(workers, ctx, monkeypatch, tmp_path):
    started = subagents.task(tasks=["alpha"], agent_id="worker", background=True, context=ctx)
    job = _job(_handle(started), ctx)
    workers.release.set()
    job.future.result(timeout=5)

    text, again = _sent(monkeypatch, tmp_path, "next", "again")
    assert text.startswith("next")
    note = re.search(r"<js-reminder>(.*?)</js-reminder>", text, re.S).group(1)
    assert job.id in note and str(job.result_path) in note
    # Only once.
    assert "<js-reminder>" not in again


def test_a_result_already_read_gets_no_reminder(workers, ctx):
    handle = _handle(subagents.task(tasks=["alpha"], agent_id="worker", background=True, context=ctx))
    workers.release.set()
    subagents.task(action="wait", handle=handle, timeout=5, context=ctx)
    assert _notes(ctx) == []


def test_a_subagents_own_background_task_is_not_reminded_to_the_operator(workers, ctx):
    child = _subagent_ctx()
    handle = _handle(subagents.task(tasks=["alpha"], agent_id="worker", background=True, context=child))
    workers.release.set()
    _job(handle, child).future.result(timeout=5)
    assert _notes(ctx) == []
    assert _notes(child) == []


def test_each_context_sees_only_its_own_tasks(workers, ctx):
    handle = _handle(subagents.task(tasks=["alpha"], agent_id="worker", background=True, context=ctx))
    child = _subagent_ctx()
    # The subagent cannot reach the main agent's task, by handle or by default.
    assert subagents.task(action="poll", handle=handle, context=child).startswith("ERROR")
    assert subagents.task(action="poll", context=child).startswith("ERROR")
    assert subagents.task(action="kill", handle=handle, context=child).startswith("ERROR")
    own = _handle(subagents.task(tasks=["beta"], agent_id="worker", background=True, context=child))
    # Without a handle the main agent gets its own task, not the newer one.
    assert f"HANDLE {handle} RUNNING" in subagents.task(action="poll", context=ctx)
    assert task_jobs.find(own, ctx.task_owner) is None

    workers.release.set()
    _job(handle, ctx).future.result(timeout=5)
    _job(own, child).future.result(timeout=5)
    notes = _notes(ctx)
    assert len(notes) == 1 and handle in notes[0]


def test_reset_drops_the_reminder_for_tasks_of_the_old_conversation(workers, ctx, monkeypatch, tmp_path):
    handle = _handle(subagents.task(tasks=["alpha"], agent_id="worker", background=True, context=ctx))
    workers.release.set()
    _job(handle, ctx).future.result(timeout=5)
    [sent] = _sent(monkeypatch, tmp_path, "/reset", "next")
    assert "<js-reminder>" not in sent


def test_finished_subagent_tasks_do_not_pile_up(workers, ctx):
    workers.release.set()
    child = _subagent_ctx()
    for _ in range(task_jobs.KEEP_FINISHED_JOBS + 4):
        handle = _handle(subagents.task(tasks=["alpha"], agent_id="worker", background=True, context=child))
        _job(handle, child).future.result(timeout=5)
    time.sleep(0.05)
    assert len(task_jobs._JOBS) <= task_jobs.KEEP_FINISHED_JOBS + 1


def test_foreground_task_still_returns_the_result(workers, ctx):
    workers.release.set()
    assert subagents.task(tasks=["alpha"], agent_id="worker", background=False, context=ctx) == "result of alpha"


def test_under_the_supervisor_the_task_outlives_the_turn_that_started_it(workers, ctx, loop_sup):
    loop, sup = loop_sup

    async def turn() -> str:
        return await subagents.task_async(tasks=["alpha"], agent_id="worker", background=True, context=ctx)

    started = asyncio.run_coroutine_threadsafe(turn(), loop).result(timeout=5)
    handle = _handle(started)
    assert workers.started.wait(5)
    assert [job.kind for job in sup.jobs()] == ["subagent"]

    async def wait() -> str:
        return await subagents.task_async(action="wait", handle=handle, timeout=10, context=ctx)

    threading.Timer(0.2, workers.release.set).start()
    finished = asyncio.run_coroutine_threadsafe(wait(), loop).result(timeout=15)
    assert "result of alpha" in finished


def test_a_sync_call_under_the_supervisor_runs_the_task_on_the_repl_loop(workers, ctx, loop_sup):
    _loop, sup = loop_sup
    # A dispatch thread calls the sync tool while the REPL loop is live.
    started = subagents.task(tasks=["alpha"], agent_id="worker", background=True, context=ctx)
    handle = _handle(started)
    assert workers.started.wait(5)
    deadline = time.monotonic() + 5
    while not sup.jobs() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert [job.kind for job in sup.jobs()] == ["subagent"]
    assert "RUNNING" in subagents.task(action="poll", handle=handle, context=ctx)
    threading.Timer(0.2, workers.release.set).start()
    assert "result of alpha" in subagents.task(action="wait", handle=handle, timeout=10, context=ctx)


def test_kill_of_a_sync_started_task_under_the_supervisor_cancels_it(workers, ctx, loop_sup):
    handle = _handle(subagents.task(tasks=["alpha"], agent_id="worker", background=True, context=ctx))
    assert workers.started.wait(5)
    subagents.task(action="kill", handle=handle, context=ctx)
    assert workers.cancelled.wait(5)
    assert _job(handle, ctx).cancelled()


def test_the_runtime_dispatch_passes_background_and_handle_through(workers, ctx, loop_sup, tmp_path, monkeypatch):
    loop, _sup = loop_sup
    registry = ToolRegistry(subagents.tools(), {})

    def dispatch(call_id: str, args: str) -> str:
        turn = run_turn_async(tmp_path, monkeypatch, registry, [[(call_id, "task", args)]], context=ctx)
        return asyncio.run_coroutine_threadsafe(turn, loop).result(timeout=10).results()[call_id]

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
        return await subagents.task_async(context=ctx, **kwargs)

    started = asyncio.run_coroutine_threadsafe(
        call(tasks=["alpha"], agent_id="worker", background=True), loop).result(timeout=5)
    assert workers.started.wait(5)
    began = time.monotonic()
    handle = _handle(started)
    killed = asyncio.run_coroutine_threadsafe(call(action="kill", handle=handle), loop).result(timeout=10)
    assert handle in killed and "RUNNING" not in killed
    assert time.monotonic() - began < task_jobs.KILL_GRACE_S
    assert workers.cancelled.wait(5)


def test_background_task_accepts_its_scheduler(monkeypatch):
    from concurrent.futures import Future

    monkeypatch.setattr(task_jobs, "_JOBS", {})
    scheduled = []
    def schedule(coro, label, settle):
        scheduled.append(label)
        outcome = Future()
        outcome.set_result(asyncio.run(coro))
        settle(outcome)
        return outcome.cancel
    async def worker(idx, item):
        return item.upper()
    job = task_jobs.start([(1, "work")], worker, lambda results: results[0],
                          agent_id="worker", depth=1, owner="caller", schedule=schedule)
    assert len(scheduled) == 1
    assert job.result() == "WORK"
    assert job.result_path.read_text() == "WORK"


def _on_loop(loop, coro, timeout: float = 10):
    return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=timeout)


def test_an_idle_repl_wakes_the_agent_with_a_finished_result(workers, ctx, monkeypatch, tmp_path):
    cfg = offline_config(tmp_path)
    cfg.prompts_dir.mkdir(parents=True, exist_ok=True)
    (cfg.prompts_dir / "01-prompt.md").write_text("sys\n", encoding="utf-8")
    turns = record_turns(monkeypatch)

    async def lines(_on_line) -> None:
        subagents.task(tasks=["alpha"], agent_id="worker", background=True, context=ctx)
        workers.release.set()
        deadline = time.monotonic() + 10
        while not turns and time.monotonic() < deadline:
            await asyncio.sleep(0.01)

    run_repl(monkeypatch, cfg, lines)
    [turn] = turns
    sent = str(turn.messages[-1]["content"])
    assert "result of alpha" in sent


def test_an_idle_repl_with_subagent_wake_off_waits_for_the_next_line(workers, ctx, monkeypatch, tmp_path):
    cfg = offline_config(tmp_path)
    cfg.prompts_dir.mkdir(parents=True, exist_ok=True)
    (cfg.prompts_dir / "01-prompt.md").write_text("sys\n", encoding="utf-8")
    turns = record_turns(monkeypatch)

    async def lines(on_line) -> None:
        await on_line("/set SUBAGENT_WAKE off")
        started = subagents.task(tasks=["alpha"], agent_id="worker", background=True, context=ctx)
        workers.release.set()
        await asyncio.wait_for(asyncio.wrap_future(_job(_handle(started), ctx).future), 5)
        await asyncio.sleep(0.2)
        assert turns == []
        await on_line("next")

    run_repl(monkeypatch, cfg, lines)
    [turn] = turns
    sent = str(turn.messages[-1]["content"])
    assert sent.startswith("next") and "result of alpha" in sent


def test_a_prompt_run_gives_the_agent_each_background_result_before_it_ends(workers, ctx):
    from js import prompt_run

    sent: list[dict] = []

    class Chat:
        context = ctx

        async def turn(self, message, *, started=None, **_kwargs):
            if started is not None:
                started()
            sent.append(message)

    prepared = types.SimpleNamespace(cfg=types.SimpleNamespace(settings={}),
                                     inputs=types.SimpleNamespace(save=False))
    subagents.task(tasks=["alpha"], agent_id="worker", background=True, context=ctx)
    threading.Timer(0.2, workers.release.set).start()
    prompt_run._background_turns(prepared, Chat(), None, None, False, {})
    [message] = sent
    assert "result of alpha" in message["content"]
    assert task_jobs.outstanding(ctx.task_owner) == []



class Chatty:
    """Child turns that read what is sent to them: each keeps a transcript in
    its worker, answers a sent "finish" by ending, and a resumed session
    answers its message at once."""

    def __init__(self, tmp_path) -> None:
        self.tmp_path = tmp_path
        self.heard: list[str] = []
        self.cancelled: list[int] = []

    async def run(self, idx, total, item, context, parent_cfg, full_registry, agent_id, session_id, model=""):
        if session_id:
            return f"resumed {session_id}: {item}"
        worker = task_jobs.CURRENT_WORKER.get()
        worker.session_file = self.tmp_path / f"worker-{idx}.jsonl"
        worker.messages = [{"role": "user", "content": item},
                           {"role": "assistant", "content": f"working on {item}",
                            "tool_calls": [{"function": {"name": "read"}}]}]
        try:
            while True:
                sent = worker.take_inbox()
                if sent is not None:
                    self.heard.append(sent["content"])
                    if "finish" in sent["content"]:
                        return f"result of {item}"
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            self.cancelled.append(idx)
            raise


@pytest.fixture
def chatty(monkeypatch, tmp_path):
    fake = Chatty(tmp_path)
    monkeypatch.setattr(subagents, "_run_one_task_async", fake.run)
    monkeypatch.setattr(task_jobs, "_JOBS", {})
    return fake


def _until(check, what: str, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, what
        time.sleep(0.01)


def test_the_main_agent_gets_a_handle_at_once_by_default(workers, ctx):
    started = subagents.task(tasks=["alpha", "beta"], agent_id="worker", context=ctx)
    job = _job(_handle(started), ctx)
    assert job.running()
    assert [w.handle for w in job.workers] == [f"{job.id}.1", f"{job.id}.2"]


def test_a_context_nothing_hands_results_to_waits_by_default(workers, ctx):
    workers.release.set()
    assert subagents.task(tasks=["alpha"], agent_id="worker", context=_ctx()) == "result of alpha"


def test_a_subagent_waits_for_its_own_subagents_by_default(workers, ctx):
    workers.release.set()
    assert subagents.task(tasks=["alpha"], agent_id="worker", context=_subagent_ctx()) == "result of alpha"


def test_poll_shows_each_subagent_at_work(chatty, ctx):
    job = _job(_handle(subagents.task(tasks=["alpha", "beta"], agent_id="worker", context=ctx)), ctx)
    _until(lambda: all(w.messages for w in job.workers), "workers started")
    polled = subagents.task(action="poll", handle=job.id, context=ctx)
    for worker in job.workers:
        assert worker.handle in polled and str(worker.session_file) in polled
    assert "working on beta" in polled and "read" in polled
    one = subagents.task(action="poll", handle=f"{job.id}.2", context=ctx)
    assert "working on beta" in one and "working on alpha" not in one
    listed = subagents.task(action="list", context=ctx)
    assert job.id in listed and f"{job.id}.1" in listed


def test_send_reaches_a_running_subagent(chatty, ctx):
    job = _job(_handle(subagents.task(tasks=["alpha"], agent_id="worker", context=ctx)), ctx)
    _until(lambda: job.workers[0].messages, "worker started")
    assert not subagents.task(action="send", handle=job.id, message="please finish", context=ctx).startswith("ERROR")
    assert job.future.result(timeout=5) == "result of alpha"
    assert chatty.heard == ["please finish"]


def test_send_to_one_of_several_needs_its_handle(chatty, ctx):
    job = _job(_handle(subagents.task(tasks=["alpha", "beta"], agent_id="worker", context=ctx)), ctx)
    assert subagents.task(action="send", handle=job.id, message="hi", context=ctx).startswith("ERROR")
    subagents.task(action="send", handle=f"{job.id}.2", message="finish", context=ctx)
    _until(lambda: job.workers[1].state == "done", "second worker done")
    assert job.workers[0].running()


def test_send_to_a_finished_subagent_continues_its_session(chatty, ctx):
    job = _job(_handle(subagents.task(tasks=["alpha"], agent_id="worker", context=ctx)), ctx)
    _until(lambda: job.workers[0].messages, "worker started")
    subagents.task(action="send", handle=job.id, message="finish", context=ctx)
    job.future.result(timeout=5)
    again = _job(_handle(subagents.task(action="send", handle=f"{job.id}.1", message="and the tests?",
                                        context=ctx)), ctx)
    assert again.id != job.id
    assert again.future.result(timeout=5) == f"resumed {job.workers[0].session_file}: and the tests?"


def test_kill_of_one_subagent_leaves_the_others_running(chatty, ctx):
    job = _job(_handle(subagents.task(tasks=["alpha", "beta"], agent_id="worker", context=ctx)), ctx)
    _until(lambda: all(w.messages for w in job.workers), "workers started")
    stopped = subagents.task(action="kill", handle=f"{job.id}.1", context=ctx)
    assert not stopped.startswith("ERROR")
    _until(lambda: job.workers[0].state == "stopped", "first worker stopped")
    assert job.workers[1].running() and job.running()
    subagents.task(action="send", handle=f"{job.id}.2", message="finish", context=ctx)
    result = job.future.result(timeout=5)
    assert "result of beta" in result and "STOPPED" in result
    assert chatty.cancelled == [1]


def _agent(tmp_path, name: str = "worker") -> None:
    agent = tmp_path / ".js" / "agents" / name
    agent.mkdir(parents=True)
    (agent / "agent.yaml").write_text("tools: []\n", encoding="utf-8")
    (agent / "01-prompt.md").write_text("WORKER\n", encoding="utf-8")


def _real_turns(monkeypatch, tmp_path, turn) -> ToolContext:
    """A main agent's context whose subagents run their own turns through
    ``turn``, in place of the model."""
    from js.config import from_env

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_DEFAULT_AGENT", raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(task_jobs, "_JOBS", {})
    monkeypatch.setattr(runtime, "run_turn_async", turn)
    _agent(tmp_path)
    main = ToolContext(cwd=tmp_path)
    main.config = from_env()
    main.delivers_task_results = True
    return main


def test_a_subagent_that_launches_its_own_stays_the_one_its_handle_reaches(monkeypatch, tmp_path, loop_sup):
    loop, _sup = loop_sup
    release = asyncio.Event()
    inner_ended = threading.Event()

    async def turn(cfg, system, messages, telemetry, *, tool_context=None, **kwargs):
        if messages[-1]["content"] == "outer":
            inner = await subagents.task_async(tasks=["inner"], agent_id="worker", context=tool_context)
            inner_ended.set()
            await release.wait()
            messages.append({"role": "assistant", "content": f"outer saw {inner}"})
        else:
            messages.append({"role": "assistant", "content": "inner answer"})

    main = _real_turns(monkeypatch, tmp_path, turn)

    async def launch() -> str:
        return await subagents.task_async(tasks=["outer"], agent_id="worker", context=main)

    handle = _handle(asyncio.run_coroutine_threadsafe(launch(), loop).result(timeout=10))
    [worker] = _job(handle, main).workers
    assert inner_ended.wait(10)

    # The inner subagent has come and gone: the handle still shows the outer one.
    assert [m["content"] for m in worker.messages if m["role"] == "user"] == ["outer"]
    outer_session = worker.session_file
    loop.call_soon_threadsafe(release.set)
    assert _job(handle, main).future.result(timeout=10) == "outer saw inner answer"
    assert worker.session_file == outer_session


def test_a_message_sent_after_a_subagents_last_tool_call_is_its_next_turn(monkeypatch, tmp_path, loop_sup):
    loop, _sup = loop_sup
    answering = threading.Event()
    release = asyncio.Event()

    async def turn(cfg, system, messages, telemetry, **kwargs):
        # No tool call, so no tool boundary reads what is sent meanwhile.
        if len([m for m in messages if m["role"] == "user"]) == 1:
            answering.set()
            await release.wait()
        messages.append({"role": "assistant", "content": f"answer to {messages[-1]['content']}"})

    main = _real_turns(monkeypatch, tmp_path, turn)

    async def launch() -> str:
        return await subagents.task_async(tasks=["alpha"], agent_id="worker", context=main)

    handle = _handle(asyncio.run_coroutine_threadsafe(launch(), loop).result(timeout=10))
    assert answering.wait(10)
    assert subagents.task(action="send", handle=handle, message="and beta", context=main).startswith("sent to")
    loop.call_soon_threadsafe(release.set)

    assert _job(handle, main).future.result(timeout=10) == "answer to and beta"
    [worker] = _job(handle, main).workers
    assert not worker.send("too late")


def test_a_job_worker_is_claimed_once_and_not_by_what_that_turn_starts():
    seen: list = []

    async def main() -> None:
        worker = object()
        task_jobs.CURRENT_WORKER.set(worker)
        seen.append(task_jobs.claim_worker() is worker)
        seen.append(await asyncio.ensure_future(_claims()))

    async def _claims():
        return task_jobs.claim_worker()

    asyncio.run(main())
    assert seen == [True, None]


def test_a_result_due_with_a_typed_line_that_cannot_be_sent_still_reaches_the_model(workers, ctx, monkeypatch, tmp_path):
    from js import attach

    cfg = offline_config(tmp_path)
    cfg.prompts_dir.mkdir(parents=True, exist_ok=True)
    (cfg.prompts_dir / "01-prompt.md").write_text("sys\n", encoding="utf-8")
    steered: list = []
    entered, release = asyncio.Event(), asyncio.Event()

    async def turn(cfg, system, messages, telemetry, *, steer=None, **kwargs):
        if not entered.is_set():
            entered.set()
            await release.wait()
            steered.append(await steer())

    build = attach.build_user_message

    def refusing(text, *args, **kwargs):
        if "broken" in text:
            raise ValueError("no such attachment")
        return build(text, *args, **kwargs)

    monkeypatch.setattr(runtime, "run_turn_async", turn)
    monkeypatch.setattr(attach, "build_user_message", refusing)

    async def lines(on_line) -> None:
        await on_line("first")
        await asyncio.wait_for(entered.wait(), 10)
        job = _job(_handle(subagents.task(tasks=["alpha"], agent_id="worker", background=True, context=ctx)), ctx)
        workers.release.set()
        while job.running():
            await asyncio.sleep(0.01)
        await on_line("a broken line")
        release.set()

    run_repl(monkeypatch, cfg, lines)

    [message] = steered
    assert "result of alpha" in message["content"]
