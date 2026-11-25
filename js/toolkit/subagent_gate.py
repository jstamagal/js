"""The process-wide subagent gate: how many subagents run at once (SUBAGENT_WORKERS_MAX), across event loops, and the slot a subagent lends its own subagents while it waits on them."""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass


class _Waiter:
    __slots__ = ("loop", "future", "limit", "granted")

    def __init__(self, loop: asyncio.AbstractEventLoop, limit: int) -> None:
        self.loop = loop
        self.future: asyncio.Future[None] = loop.create_future()
        self.limit = limit
        self.granted = False


class SubagentGate:
    """Process-wide count of running subagents, held to SUBAGENT_WORKERS_MAX.

    Subagents run on more than one event loop (the REPL's, a `-p` run's, a
    background job's private loop), so the count sits behind a thread lock and
    a waiter is woken on its own loop. Waiters are served in arrival order.
    Each acquire carries the limit its caller's settings give."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running = 0
        self._waiters: deque[_Waiter] = deque()

    @property
    def running(self) -> int:
        return self._running

    async def acquire(self, limit: int) -> None:
        loop = asyncio.get_running_loop()
        with self._lock:
            if not self._waiters and self._running < limit:
                self._running += 1
                return
            waiter = _Waiter(loop, limit)
            self._waiters.append(waiter)
        try:
            await waiter.future
        except BaseException:
            with self._lock:
                if waiter.granted:
                    self._release_locked()
                elif waiter in self._waiters:
                    self._waiters.remove(waiter)
                    self._wake_locked()
            raise

    def release(self) -> None:
        with self._lock:
            self._release_locked()

    def _release_locked(self) -> None:
        self._running -= 1
        self._wake_locked()

    def _wake_locked(self) -> None:
        while self._waiters and self._running < self._waiters[0].limit:
            waiter = self._waiters.popleft()
            waiter.granted = True
            self._running += 1
            try:
                waiter.loop.call_soon_threadsafe(_grant, waiter.future)
            except RuntimeError:  # its loop is closed: nobody is waiting
                waiter.granted = False
                self._running -= 1


def _grant(future: asyncio.Future[None]) -> None:
    if not future.done():
        future.set_result(None)


GATE = SubagentGate()


@dataclass
class Slot:
    """The gate slot one running subagent holds. While it waits on its own
    subagents it lends the slot to them, so a parent blocked on its children
    never holds a place they need."""

    limit: int
    held: bool = True
    lent: int = 0
    # False once its subagent run is over; a later turn in its window runs
    # outside the gate.
    active: bool = True


# The slot of the subagent this coroutine runs in; None in the main agent.
SLOT: ContextVar[Slot | None] = ContextVar("js_subagent_slot", default=None)


@asynccontextmanager
async def lend_slot():
    """Give up this subagent's slot while it waits on subagents it started."""
    slot = SLOT.get()
    if slot is None or not slot.active:
        yield
        return
    slot.lent += 1
    if slot.held:
        slot.held = False
        GATE.release()
    try:
        yield
    except Exception:
        await _take_back(slot)
        raise
    except BaseException:
        # Cancelled: the subagent is ending, it needs no slot to end.
        slot.lent -= 1
        raise
    else:
        await _take_back(slot)


async def _take_back(slot: Slot) -> None:
    slot.lent -= 1
    if slot.lent == 0 and not slot.held and slot.active:
        await GATE.acquire(slot.limit)
        if slot.lent or slot.held or not slot.active:
            GATE.release()
        else:
            slot.held = True
