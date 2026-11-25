"""The REPL's job commands: /jobs, /cancel and /flush, over the supervisor's
jobs."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from . import messages as msgs, supervisor
from .config import Config
from .repl_state import Command, ReplState


def _cmd_jobs(arg: str, state: ReplState, cfg: Config) -> str | None:
    sup = supervisor.get_current()
    if sup is None:
        msgs.say(msgs.REPL_ONLY, command="/jobs")
        return None
    jobs = sup.jobs()
    if not jobs:
        msgs.say(msgs.NO_JOBS)
    for j in jobs:
        msgs.say(msgs.JOB_ROW, id=j.id, kind=j.kind, label=f"  {j.label}" if j.label else "")
    return None


def _cmd_cancel(arg: str, state: ReplState, cfg: Config) -> str | None:
    sup = supervisor.get_current()
    if sup is None:
        msgs.say(msgs.REPL_ONLY, command="/cancel")
        return None
    if arg:
        if not arg.isdigit():
            return msgs.USAGE.said(usage="/cancel [id]. Bare cancels the active turn.")
        targets = [j for j in sup.jobs() if j.id == int(arg)]
    else:
        targets = sup.jobs("turn")
    if not targets:
        msgs.say(msgs.NO_JOB_TO_CANCEL)
        return None
    # Task.cancel() is not thread-safe; hop to the loop thread to fire it.
    for j in targets:
        sup.loop.call_soon_threadsafe(sup.cancel, j.id)
    ids = ", ".join(f"[{j.id}] {j.kind}" for j in targets)
    msgs.say(msgs.CANCELLING, jobs=ids)
    return None


def _on_loop(call: Callable[[], object]) -> object:
    """``call``'s result, run on the REPL's loop from a command's executor thread."""
    sup = supervisor.get_current()
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if sup is None or running is sup.loop:
        return call()

    async def run():
        return call()

    return asyncio.run_coroutine_threadsafe(run(), sup.loop).result()


COMMANDS: dict[str, Command] = {
    "jobs": Command(_cmd_jobs, "jobs", msgs.CMD_JOBS),
    "cancel": Command(_cmd_cancel, "cancel [id]", msgs.CMD_CANCEL),
    # The async REPL drains its loop-owned queue before a line reaches the
    # table; this entry runs everywhere else, where nothing queues.
    "flush": Command(lambda arg, state, cfg: msgs.say(msgs.NO_QUEUED_PROMPTS),
                     "flush", msgs.CMD_FLUSH, stands_alone=True),
}
