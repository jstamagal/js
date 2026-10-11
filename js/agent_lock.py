"""Opt-in --lock: one locked run per agent at a time.

The lock is an flock on state/<agent>/run.lock, held until the process exits,
so a crash or kill releases it. Python opens the file non-inheritable, so
commands the agent starts do not keep it alive. Runs without --lock never
look at it.
"""

from __future__ import annotations

import fcntl

from . import paths


class Busy(Exception):
    pass


_held: list = []


def acquire(agent_id: str) -> None:
    path = paths.state_root() / agent_id / "run.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        lock.close()
        raise Busy(f"another --lock run of agent {agent_id!r} is running ({path})") from exc
    _held.append(lock)
