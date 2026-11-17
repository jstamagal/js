"""/jobs and /cancel command dispatch: listing and target selection, typed into
js while a turn and its subagent jobs run. With no supervisor the commands
say they need the REPL."""

from __future__ import annotations

import asyncio

from repl_driver import output_of, settled, type_into_js

from js import cli, messages as msgs, repl_commands, supervisor
from js.repl_state import ReplState


def _listed(lines, *fields):
    """Whether one line of a listing carries every field."""
    return any(all(field in line for field in fields) for line in lines)


def _set_sup(monkeypatch, sup):
    monkeypatch.setattr(supervisor, "get_current", lambda: sup)


def _with_jobs(monkeypatch, tmp_path, typing) -> None:
    """Start js in `tmp_path` and type `fix the bug`: a turn that runs until
    cancelled and starts the subagent jobs `task#1` and `task#2`, which do the
    same. Once all three run, `typing(enter, jobs)` gets the Enter handler and
    the live jobs by label; EOF follows when it returns."""
    monkeypatch.chdir(tmp_path)
    running = asyncio.Event()

    async def until_cancelled():
        await asyncio.Event().wait()

    async def run_turn_async(cfg, system, messages, *args, **kwargs):
        sup = supervisor.get_current()
        for label in ("task#1", "task#2"):
            sup.spawn(until_cancelled(), kind="subagent", label=label)
        running.set()
        await until_cancelled()

    monkeypatch.setattr(cli.runtime, "run_turn_async", run_turn_async)

    async def drive(enter):
        await enter("fix the bug")
        await running.wait()
        jobs = {job.label: job for job in supervisor.get_current().jobs()}
        try:
            await typing(enter, jobs)
        finally:
            for job in jobs.values():
                job.task.cancel()
            await asyncio.wait({job.task for job in jobs.values()})

    assert type_into_js(monkeypatch, drive) == 0


def _cancel_requests(jobs) -> dict[str, int]:
    """The cancel requests pending on each job, by label."""
    return {label: job.task.cancelling() for label, job in jobs.items()}


def test_jobs_lists_running(monkeypatch, tmp_path, capsys):
    listed: list[str] = []
    ids: dict[str, int] = {}

    async def typing(enter, jobs):
        ids.update({label: job.id for label, job in jobs.items()})
        listed.extend((await output_of(enter, capsys, "/jobs")).splitlines())

    _with_jobs(monkeypatch, tmp_path, typing)

    assert _listed(listed, str(ids["fix the bug"]), "turn", "fix the bug")
    assert _listed(listed, str(ids["task#1"]), "subagent", "task#1")


def test_jobs_without_supervisor(monkeypatch, capsys):
    _set_sup(monkeypatch, None)
    assert repl_commands.handle_command("/jobs", ReplState(), None) is True
    assert capsys.readouterr().out


def test_cancel_bare_targets_active_turn_only(monkeypatch, tmp_path):
    requests: dict[str, int] = {}

    async def typing(enter, jobs):
        await enter("/cancel")
        await settled()
        requests.update(_cancel_requests(jobs))

    _with_jobs(monkeypatch, tmp_path, typing)

    assert requests == {"fix the bug": 1, "task#1": 0, "task#2": 0}


def test_cancel_by_id(monkeypatch, tmp_path):
    requests: dict[str, int] = {}

    async def typing(enter, jobs):
        await enter(f"/cancel {jobs['task#1'].id}")
        await settled()
        requests.update(_cancel_requests(jobs))

    _with_jobs(monkeypatch, tmp_path, typing)

    assert requests == {"fix the bug": 0, "task#1": 1, "task#2": 0}


def test_cancel_bad_arg_shows_usage(monkeypatch, tmp_path, capsys):
    requests: dict[str, int] = {}
    printed: list[str] = []

    async def typing(enter, jobs):
        printed.append(await output_of(enter, capsys, "/cancel nope"))
        requests.update(_cancel_requests(jobs))

    _with_jobs(monkeypatch, tmp_path, typing)

    assert set(requests.values()) == {0}
    assert printed[0]


def test_cancel_unknown_id_is_noop(monkeypatch, tmp_path, capsys):
    requests: dict[str, int] = {}
    printed: list[str] = []

    async def typing(enter, jobs):
        printed.append(await output_of(enter, capsys, "/cancel 99"))
        requests.update(_cancel_requests(jobs))

    _with_jobs(monkeypatch, tmp_path, typing)

    assert set(requests.values()) == {0}
    assert printed[0]


def test_drain_queue_drops_all_pending_and_balances_task_done(monkeypatch, tmp_path, capsys):
    """/flush and ^C drain drop every queued prompt and balance task_done() so a
    later queue.join() still completes (no orphaned unfinished tasks)."""
    monkeypatch.chdir(tmp_path)
    started, release = asyncio.Event(), asyncio.Event()
    turns: list[str] = []
    flushed: list[str] = []

    async def run_turn_async(cfg, system, messages, *args, **kwargs):
        turns.append(messages[-1]["content"])
        started.set()
        await release.wait()

    async def typing(enter):
        await enter("/set STEER_MODE batch")
        await enter("first")
        await asyncio.wait_for(started.wait(), timeout=10)
        for line in ("one", "two", "three"):
            await enter(line)
        flushed.append(await output_of(enter, capsys, "/flush"))
        # Draining an empty queue is a clean no-op.
        flushed.append(await output_of(enter, capsys, "/flush"))
        release.set()

    monkeypatch.setattr(cli.runtime, "run_turn_async", run_turn_async)
    # The REPL ends once the queue's join() completes.
    assert type_into_js(monkeypatch, typing) == 0

    # Every item gone and counted; no queued prompt ran.
    assert msgs.plural(3, "queued prompt") in flushed[0]
    assert msgs.plural(0, "queued prompt") in flushed[1]
    assert turns == ["first"]