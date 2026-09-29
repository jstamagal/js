"""The grace window after an interrupt must survive an unresponsive kernel."""

from __future__ import annotations

import queue
import time
from pathlib import Path

from js.toolkit import kernel as kmod


class _SlowToInterruptClient:
    """A kernel that ignores SIGINT for several polls, like a CPU-bound C
    extension that only checks signals between chunks."""

    def __init__(self, quiet_polls: int):
        self.quiet_polls = quiet_polls
        self.polls = 0

    def get_shell_msg(self, timeout: float):
        raise queue.Empty

    def get_iopub_msg(self, timeout: float):
        self.polls += 1
        if self.polls <= self.quiet_polls:
            raise queue.Empty
        if self.polls == self.quiet_polls + 1:
            return {"parent_header": {"msg_id": "m1"},
                    "header": {"msg_type": "error"},
                    "content": {"traceback": ["KeyboardInterrupt"]}}
        return {"parent_header": {"msg_id": "m1"},
                "header": {"msg_type": "status"},
                "content": {"execution_state": "idle"}}


class _Manager:
    def __init__(self):
        self.interrupts = 0

    def interrupt_kernel(self):
        self.interrupts += 1


def _session(client, alive=True):
    session = kmod.KernelSession(cwd=Path("."), artifacts=Path("."))
    session.client = client
    session.manager = _Manager()
    session.alive = lambda: alive
    return session


def _handle(session, msg_id="m1"):
    handle = kmod.CellHandle(id="1", msg_id=msg_id, code="slow()", started=time.monotonic())
    session.handles[handle.id] = handle
    session.current = handle.id
    return handle


def test_the_grace_window_keeps_polling_for_the_keyboardinterrupt(monkeypatch):
    client = _SlowToInterruptClient(quiet_polls=4)
    session = _session(client)
    handle = _handle(session)

    kmod.interrupt_and_collect(session, handle)

    assert handle.died is False
    assert handle.finished is True
    assert session.manager.interrupts == 1
    assert [m["header"]["msg_type"] for m in handle.messages] == ["error", "status"]


def test_the_grace_window_stops_early_when_the_kernel_is_gone():
    client = _SlowToInterruptClient(quiet_polls=1000)
    session = _session(client, alive=False)
    handle = _handle(session)

    kmod.interrupt_and_collect(session, handle)

    assert handle.died is True
    assert handle.messages == []
    assert client.polls == 1


class _SilentClient:
    """A kernel that has taken a SIGINT without acting on it: nothing arrives."""

    def get_shell_msg(self, timeout: float):
        raise queue.Empty

    def get_iopub_msg(self, timeout: float):
        raise queue.Empty


def test_a_running_cell_that_ignores_the_signal_is_signalled_again(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(kmod.time, "monotonic", lambda: clock[0])
    session = _session(_SilentClient())
    handle = _handle(session)
    handle.running = True

    kmod.signal_cell(session, handle)
    kmod.resignal_if_still_running(session, handle)
    assert session.manager.interrupts == 1

    clock[0] += kmod.RESIGNAL_INTERVAL
    kmod.resignal_if_still_running(session, handle)
    assert session.manager.interrupts == 2


def test_only_a_signalled_running_unfinished_cell_is_signalled_again(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(kmod.time, "monotonic", lambda: clock[0])
    session = _session(_SilentClient())
    never_signalled = _handle(session, msg_id="a")
    never_signalled.running = True
    not_started = _handle(session, msg_id="b")
    kmod.signal_cell(session, not_started)
    finished = _handle(session, msg_id="c")
    finished.running = True
    kmod.signal_cell(session, finished)
    finished.finished = True
    assert session.manager.interrupts == 2

    clock[0] += 10 * kmod.RESIGNAL_INTERVAL
    for handle in (never_signalled, not_started, finished):
        kmod.resignal_if_still_running(session, handle)

    assert session.manager.interrupts == 2
