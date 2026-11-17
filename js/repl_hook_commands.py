"""The REPL's event-hook commands: /on, which runs a command on an event,
and /exec, the shell command a handler runs (or one typed at the prompt),
its stdout queued for the next user message."""

from __future__ import annotations

import os

from . import (
    events,
    hookexec,
    messages as msgs,
    repl_state,
    runtime,
    setcmd,
    settings,
)
from .config import Config
from .repl_state import Command, ReplState


def _cmd_on(arg: str, state: ReplState, cfg: Config) -> str | None:
    if state.events is None:
        state.events = events.EventHooks()
    result = setcmd.on_command(state.events, arg)
    for out in result.lines:
        print(out)
    return result.error


def _cmd_exec(arg: str, state: ReplState | None, cfg: Config) -> str | None:
    """Run ``arg`` in the shell and queue its stdout for the next user message;
    with no ``state`` there is no next message and the stdout goes nowhere.
    Run by an `on` handler, the command reads the event as JSON on stdin with
    JS_EVENT naming it. Exit status 2 or a timeout under tool_call refuses the call."""
    command = arg.strip()
    if not command:
        return msgs.USAGE.said(usage="/exec <command>")
    call = events.current_call()
    live = state.settings if state is not None else cfg.settings
    timeout = int(settings.knob(live, "EXEC_TIMEOUT"))
    env = dict(os.environ)
    stdin_text = ""
    if call is not None:
        env["JS_EVENT"] = call.emission.event
        stdin_text = hookexec.event_json(call)
    try:
        outcome = hookexec.run(
            command,
            cwd=str(runtime.T.STOCK_CONTEXT.cwd),
            timeout=timeout,
            cap=int(settings.knob(live, "EXEC_OUTPUT_MAX") or 0),
            stdin_text=stdin_text,
            env=env,
        )
    except OSError as e:
        failure = msgs.EXEC_FAILED.said(command=command, error=e)
    else:
        if outcome.returncode is None:
            failure = msgs.EXEC_TIMED_OUT.said(command=command, seconds=timeout)
            hookexec.refuse_unfinished(call, str(failure))
        elif hookexec.apply_refusal(call, outcome):
            return None
        elif outcome.returncode != 0:
            detail = next((line.strip() for line in (outcome.stderr or outcome.stdout).splitlines()
                           if line.strip()), "")
            failure = msgs.EXEC_EXITED.said(command=command, status=outcome.returncode, detail=detail)
        else:
            note = hookexec.reminder(outcome.stdout)
            if note is not None and state is not None:
                repl_state.queue_note(state, note)
            if call is None:
                # Typed at the prompt: show what the model will read.
                if note is None:
                    msgs.say(msgs.EXEC_NO_OUTPUT)
                else:
                    print(outcome.stdout.rstrip("\n"))
                    msgs.say(msgs.EXEC_QUEUED, lines=msgs.plural(len(outcome.stdout.strip().splitlines()), "line"))
            return None
    if call is not None:
        # An `on` handler's errors otherwise reach only the debug log.
        msgs.say_said(failure)
    return failure


COMMANDS: dict[str, Command] = {
    "on": Command(_cmd_on, "on [[^]event command]", msgs.CMD_ON, complete="events"),
    "exec": Command(_cmd_exec, "exec <command>", msgs.CMD_EXEC, complete="path", stands_alone=True),
}
