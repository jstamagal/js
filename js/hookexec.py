"""`/exec CMD`: run a shell command for the command layer.

Its stdout reaches the model once, as a js-reminder on the next user message.
Run as an `on` handler, the command reads the event on stdin as one JSON
object, and under a tool_call handler an exit status of 2 refuses the call:
the refusal line is what the model reads as the call's result.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
from dataclasses import dataclass

from . import events

# The exit status that refuses a refusable event (Claude Code's blocking status).
REFUSE_STATUS = 2


@dataclass(frozen=True)
class ExecOutcome:
    returncode: int | None  # None when the command was killed at the timeout
    stdout: str
    stderr: str


def _clip(data: bytes, cap: int) -> str:
    return (data[:cap] if cap > 0 else data).decode("utf-8", errors="replace")


def run(command: str, *, cwd: str, timeout: float, cap: int,
        stdin_text: str = "", env: dict[str, str] | None = None) -> ExecOutcome:
    """Run ``command`` under ``$SHELL -c`` (``/bin/sh`` without one) in its own
    process group, ``stdin_text`` on stdin. At ``timeout`` seconds the whole
    group is killed. Each stream keeps its first ``cap`` bytes; 0 keeps all."""
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
    try:
        out, err = proc.communicate(stdin_text.encode("utf-8"), timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        out, err = proc.communicate()
        return ExecOutcome(None, _clip(out, cap), _clip(err, cap))
    return ExecOutcome(proc.returncode, _clip(out, cap), _clip(err, cap))


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
