"""/jobs and /cancel command dispatch: listing, target selection, and scheduling
cancels onto the loop thread. Uses a fake supervisor — the real loop + threadsafe
cancel are covered in test_supervisor.py."""

from __future__ import annotations

from js import cli


class _FakeJob:
    def __init__(self, jid, kind, label=""):
        self.id = jid
        self.kind = kind
        self.label = label


class _FakeLoop:
    def __init__(self):
        self.scheduled: list[tuple] = []

    def call_soon_threadsafe(self, fn, *args):
        self.scheduled.append((fn, args))


class _FakeSup:
    def __init__(self, jobs):
        self._jobs = jobs
        self.loop = _FakeLoop()
        self.cancelled: list[int] = []

    def jobs(self, kind=None):
        return [j for j in self._jobs if kind is None or j.kind == kind]

    def cancel(self, jid):
        self.cancelled.append(jid)


def _set_sup(monkeypatch, sup):
    monkeypatch.setattr(cli.supervisor, "get_current", lambda: sup)


def test_jobs_lists_running(monkeypatch, capsys):
    _set_sup(monkeypatch, _FakeSup([_FakeJob(1, "turn", "fix the bug"), _FakeJob(2, "subagent", "task#1")]))
    assert cli._handle_command("/jobs", {}, None) is True
    out = capsys.readouterr().out
    assert "[1] turn" in out and "fix the bug" in out
    assert "[2] subagent" in out and "task#1" in out


def test_jobs_without_supervisor(monkeypatch, capsys):
    _set_sup(monkeypatch, None)
    assert cli._handle_command("/jobs", {}, None) is True
    assert "nonblocking" in capsys.readouterr().out


def test_cancel_bare_targets_active_turn_only(monkeypatch, capsys):
    sup = _FakeSup([_FakeJob(1, "turn"), _FakeJob(2, "subagent"), _FakeJob(3, "subagent")])
    _set_sup(monkeypatch, sup)
    assert cli._handle_command("/cancel", {}, None) is True
    # Only the turn was scheduled for cancel; run the scheduled callback to confirm.
    assert [args for _fn, args in sup.loop.scheduled] == [(1,)]
    for fn, args in sup.loop.scheduled:
        fn(*args)
    assert sup.cancelled == [1]
    assert "[1] turn" in capsys.readouterr().out


def test_cancel_by_id(monkeypatch, capsys):
    sup = _FakeSup([_FakeJob(1, "turn"), _FakeJob(2, "subagent", "task#1")])
    _set_sup(monkeypatch, sup)
    assert cli._handle_command("/cancel 2", {}, None) is True
    assert [args for _fn, args in sup.loop.scheduled] == [(2,)]
    assert "[2] subagent" in capsys.readouterr().out


def test_cancel_bad_arg_shows_usage(monkeypatch, capsys):
    sup = _FakeSup([_FakeJob(1, "turn")])
    _set_sup(monkeypatch, sup)
    assert cli._handle_command("/cancel nope", {}, None) is True
    assert not sup.loop.scheduled
    assert "usage:" in capsys.readouterr().out


def test_cancel_unknown_id_is_noop(monkeypatch, capsys):
    sup = _FakeSup([_FakeJob(1, "turn")])
    _set_sup(monkeypatch, sup)
    assert cli._handle_command("/cancel 99", {}, None) is True
    assert not sup.loop.scheduled
    assert "no matching job" in capsys.readouterr().out


def test_drain_queue_drops_all_pending_and_balances_task_done():
    """/flush and ^C drain drop every queued prompt and balance task_done() so a
    later queue.join() still completes (no orphaned unfinished tasks)."""
    import asyncio

    async def drive():
        queue: asyncio.Queue = asyncio.Queue()
        for line in ("one", "two", "three"):
            queue.put_nowait(line)
        dropped = cli._drain_queue(queue)
        # Every item gone, count returned, and join() unblocks immediately.
        assert dropped == 3
        assert queue.qsize() == 0
        await asyncio.wait_for(queue.join(), timeout=1.0)
        # Draining an empty queue is a clean no-op.
        assert cli._drain_queue(queue) == 0

    asyncio.run(drive())
