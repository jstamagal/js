"""`/exec CMD`: run a shell command for the command layer.

Its stdout reaches the model once, as a js-reminder on the next user message.
Run as an `on` handler, the command reads the event on stdin as one JSON
object, and under a tool_call handler an exit status of 2 refuses the call:
the refusal line is what the model reads as the call's result.
"""

from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import time
from dataclasses import dataclass

from . import events

# The exit status that refuses a refusable event (Claude Code's blocking status).
REFUSE_STATUS = 2


@dataclass(frozen=True)
class ExecOutcome:
    returncode: int | None  # None when the command was killed at the timeout
    stdout: str
    stderr: str


# After the shell exits, output from anything it left running in the
# background is read for this long before the pipes are let go.
DRAIN_S = 0.2
_POLL_S = 0.05


def _clip(data: bytes, cap: int) -> str:
    return (bytes(data[:cap]) if cap > 0 else bytes(data)).decode("utf-8", errors="replace")


def run(command: str, *, cwd: str, timeout: float, cap: int,
        stdin_text: str = "", env: dict[str, str] | None = None) -> ExecOutcome:
    """Run ``command`` under ``$SHELL -c`` (``/bin/sh`` without one) in its own
    process group, ``stdin_text`` on stdin, and return by ``timeout`` seconds
    (0 is no limit). At the timeout the whole group is killed. Once the shell
    exits, what it left in the background keeps running and its output stops
    being read after DRAIN_S. Each stream keeps its first ``cap`` bytes; 0
    keeps all."""
    shell = os.environ.get("SHELL") or "/bin/sh"
    proc = subprocess.Popen(
        [shell, "-c", command],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=env,
        start_new_session=True,
    )
    deadline = time.monotonic() + timeout if timeout > 0 else None
    captured = {proc.stdout: bytearray(), proc.stderr: bytearray()}
    pending = memoryview(stdin_text.encode("utf-8"))
    selector = selectors.DefaultSelector()
    for stream in captured:
        selector.register(stream, selectors.EVENT_READ)
    if pending:
        os.set_blocking(proc.stdin.fileno(), False)
        selector.register(proc.stdin, selectors.EVENT_WRITE)
    else:
        proc.stdin.close()
    timed_out = False
    exited_at: float | None = None
    try:
        while selector.get_map():
            now = time.monotonic()
            if exited_at is None and proc.poll() is not None:
                exited_at = now
            if deadline is not None and now >= deadline and exited_at is None:
                timed_out = True
                break
            if exited_at is not None and now >= exited_at + DRAIN_S:
                break
            for key, _mask in selector.select(_POLL_S):
                stream = key.fileobj
                if stream is proc.stdin:
                    try:
                        pending = pending[os.write(stream.fileno(), pending[:65536]):]
                    except (BrokenPipeError, BlockingIOError) as e:
                        if isinstance(e, BlockingIOError):
                            continue
                        pending = pending[:0]
                    if not pending:
                        selector.unregister(stream)
                        stream.close()
                    continue
                data = os.read(stream.fileno(), 65536)
                if not data:
                    selector.unregister(stream)
                    continue
                kept = captured[stream]
                if cap <= 0 or len(kept) < cap:
                    kept += data
    finally:
        selector.close()
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            stream.close()
    if not timed_out and exited_at is None:
        # Every stream closed but the shell still runs: wait out the rest.
        try:
            proc.wait(None if deadline is None else max(deadline - time.monotonic(), 0))
        except subprocess.TimeoutExpired:
            timed_out = True
    if timed_out:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
        return ExecOutcome(None, _clip(captured[proc.stdout], cap), _clip(captured[proc.stderr], cap))
    return ExecOutcome(proc.wait(), _clip(captured[proc.stdout], cap), _clip(captured[proc.stderr], cap))


def event_json(call: events.HandlerCall) -> str:
    """The event a handler answers, as the JSON object its command reads."""
    return json.dumps({"event": call.emission.event, **call.emission.payload},
                      ensure_ascii=False, default=str)


def reminder(stdout: str) -> str | None:
    """The js-reminder carrying ``stdout``; None for blank output."""
    text = stdout.strip()
    return f"<js-reminder>{text}</js-reminder>" if text else None


def refusal(outcome: ExecOutcome, tool: str) -> str:
    """The one ERROR line a refused call returns: the first non-blank line of
    stderr, else of stdout."""
    for stream in (outcome.stderr, outcome.stdout):
        for line in stream.splitlines():
            line = line.strip()
            if line:
                return line if line.startswith("ERROR") else f"ERROR: {line}"
    return f"ERROR: an on tool_call handler refused {tool}."
