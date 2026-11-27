"""Repeat complete CLI invocations as fresh, isolated processes."""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable

from . import command_line, messages as msgs, settings as _settings, snippets
from .capped_process import descendants

_DURATION = re.compile(r"([1-9][0-9]*)([mMhH])\Z")

# What a run exits with when its agent's user files end it on a control word:
# LOOP asks for another fresh run, HALT for no more of them.
LOOP_EXIT = 75
HALT_EXIT = 76
CONTROL_EXITS = (LOOP_EXIT, HALT_EXIT)
# Set in the environment of a run the controller starts. That run takes it out
# of its environment as it starts, so the commands it runs do not inherit it.
_CHILD_ENV = "JS_LOOP_CHILD"
_is_child = False


def claim_child() -> None:
    """Note whether a loop controller started this process."""
    global _is_child
    _is_child = os.environ.pop(_CHILD_ENV, None) is not None


def after_run(code: int, argv: list[str], *, bench: str | None, prompt: str | None,
              settings: Callable[[], dict],
              relay: Callable[[Iterable[str]], None] | None = None) -> int:
    """The exit code of a run that ended with ``code``. A run a controller
    started hands its control exit to that controller. A run started by hand
    is its own controller: HALT ends it, and LOOP repeats ``argv`` as fresh
    runs for as long as each one ends on LOOP. ``relay`` takes the stdout
    lines of each repeat."""
    if code not in CONTROL_EXITS or _is_child:
        return code
    if code == HALT_EXIT:
        return 0
    return run(argv, "", bench=bench, prompt=prompt, settings=settings(), while_looping=True,
               relay=relay)


def parse_limit(value: str) -> tuple[int | None, float | None]:
    if value == "":
        return None, None
    if value.isdecimal() and int(value) > 0:
        return int(value), None
    match = _DURATION.fullmatch(value)
    if match:
        return None, int(match[1]) * (60 if match[2].lower() == "m" else 3600)
    raise ValueError(msgs.LOOP_LOOP_EXPECTS_A_POSITIVE_TURN_COUNT_MINUTES.format())


def child_argv(argv: list[str]) -> list[str]:
    """`argv` without its --loop options: the command of one run of the loop."""
    return command_line.words([arg for arg in command_line.read(argv) if arg.dest != "loop"])


def _detached_descendant_groups(root_pid: int) -> set[int]:
    own_group = os.getpgrp()
    return {group for _, group in descendants(root_pid) if group > 0 and group not in (root_pid, own_group)}


def _stop_child(child: subprocess.Popen) -> None:
    group = child.pid
    detached = _detached_descendant_groups(group)
    for sig, grace in ((signal.SIGINT, 3), (signal.SIGTERM, 2)):
        detached.update(_detached_descendant_groups(group))
        for owned_group in (group, *detached):
            try:
                os.killpg(owned_group, sig)
            except ProcessLookupError:
                pass
        try:
            child.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass
        else:
            if sig == signal.SIGTERM:
                time.sleep(0.2)
    detached.update(_detached_descendant_groups(group))
    for owned_group in (group, *detached):
        try:
            os.killpg(owned_group, signal.SIGKILL)
        except ProcessLookupError:
            pass
    child.wait()


def run(argv: list[str], limit: str, *, bench: str | None, prompt: str | None,
        child_command: list[str] | None = None, settings: dict | None = None,
        while_looping: bool = False,
        relay: Callable[[Iterable[str]], None] | None = None) -> int:
    """Repeat ``argv`` as fresh runs up to ``limit``. A run that ends on HALT
    ends the loop. ``while_looping`` also ends it after a run that did not end
    on LOOP, and sends each run as it was typed, with no instruction added.
    With ``relay``, each run's stdout is piped and its lines handed to it."""
    count, seconds = parse_limit(limit)
    command = list(child_command or [sys.executable, "-m", "js"])
    args = child_argv(argv)
    stdin_data = sys.stdin.buffer.read() if not sys.stdin.isatty() else None
    if (prompt is None and bench is None and not stdin_data and not while_looping
            and "--commit" not in args):
        args += ["-p", snippets.render("fresh-session")]
    deadline = time.monotonic() + seconds if seconds is not None else None
    completed = failures = attempts = 0
    max_attempts = count * int(_settings.knob(settings, "LOOP_ATTEMPTS_MULTIPLIER")) if count is not None else None
    failure_limit = int(_settings.knob(settings, "LOOP_FAILURES_MAX"))
    backoff_max = int(_settings.knob(settings, "LOOP_BACKOFF_MAX"))
    try:
        while (count is None or completed < count) and (deadline is None or time.monotonic() < deadline):
            if max_attempts is not None and attempts >= max_attempts:
                print(msgs.LOOP_ATTEMPTS_EXHAUSTED.words(completed=completed, attempts=attempts), file=sys.stderr)
                return 1
            attempts += 1
            print(msgs.LOOP_ATTEMPT.words(attempts=attempts, completed=completed, value=count or msgs.LOOP_UNLIMITED), flush=True)
            if relay is None:
                child = subprocess.Popen(command + args, start_new_session=True,
                                         stdin=subprocess.PIPE if stdin_data is not None else None,
                                         env={**os.environ, _CHILD_ENV: "1"})
            else:
                # Stdin comes from a file, so the run never waits on a write
                # while its stdout is read.
                with tempfile.TemporaryFile() as stdin_file:
                    if stdin_data is not None:
                        stdin_file.write(stdin_data)
                        stdin_file.seek(0)
                    child = subprocess.Popen(command + args, start_new_session=True,
                                             stdin=stdin_file if stdin_data is not None else None,
                                             stdout=subprocess.PIPE, text=True,
                                             env={**os.environ, _CHILD_ENV: "1"})
            try:
                if relay is None:
                    child.communicate(input=stdin_data)
                else:
                    with child.stdout:
                        relay(child.stdout)
                    child.wait()
                code = child.returncode
            except BaseException:
                _stop_child(child)
                raise
            if code == HALT_EXIT or (while_looping and code == 0):
                return 0
            if code in (0, LOOP_EXIT):
                completed += 1
                failures = 0
                continue
            if code == 130:
                return 130
            failures += 1
            if count is not None or failures >= failure_limit:
                print(msgs.LOOP_CHILD_STOPPED.words(code=code, completed=completed), file=sys.stderr)
                return code or 1
            delay = min(2 ** (failures - 1), backoff_max)
            print(msgs.LOOP_CHILD_RETRY.format(code=code, delay=delay, failures=failures, failure_limit=failure_limit), file=sys.stderr)
            if deadline is not None and time.monotonic() + delay >= deadline:
                return code or 1
            time.sleep(delay)
    except KeyboardInterrupt:
        print(msgs.LOOP_STOPPED.words(), file=sys.stderr)
        return 130
    return 0
