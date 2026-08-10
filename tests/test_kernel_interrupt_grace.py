"""The grace window after an interrupt must survive an unresponsive kernel."""

from __future__ import annotations

import queue
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


def test_the_grace_window_keeps_polling_for_the_keyboardinterrupt(monkeypatch):
    client = _SlowToInterruptClient(quiet_polls=4)
    session = _session(client)

    messages, timed_out, died = kmod._collect(session, "m1", timeout=0)

    assert timed_out is True
    assert died is False
    assert session.manager.interrupts == 1
    assert [m["header"]["msg_type"] for m in messages] == ["error", "status"]


def test_the_grace_window_stops_early_when_the_kernel_is_gone():
    client = _SlowToInterruptClient(quiet_polls=1000)
    session = _session(client, alive=False)

    messages, timed_out, died = kmod._collect(session, "m1", timeout=0)

    assert timed_out is True
    assert died is True
    assert messages == []
    assert client.polls == 1
