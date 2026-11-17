"""The jsrc `on` handlers a run outside the REPL keeps: those whose command stands alone, run with no REPL state, what they print sent to stderr."""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path

from . import events, messages as msgs, repl_commands, setcmd, setting_layers
from .config import Config


def _standalone_dispatcher(cfg: Config):
    """Run `on` handler text whose command stands alone, with no REPL state.
    What the command prints goes to stderr: stdout carries the run's answer."""

    def dispatch(hook: events.EventHook, emission: events.EventEmission) -> events.EventHandlerResult:
        line = setcmd.expand_line(hook.handler, cfg.settings)
        verb, arg = setcmd.split_command(line) or ("<blank>", "")
        command = repl_commands.standalone(line)
        if command is None:
            return events.EventHandlerResult(hook=hook, error=msgs.EVENT_HANDLER_COMMAND_UNSUPPORTED.words(value=verb))
        with contextlib.redirect_stdout(sys.stderr):
            return events.EventHandlerResult(hook=hook, error=command.run(arg, None, cfg))

    return dispatch


def _admit_standalone(hook: events.EventHook) -> str | None:
    if repl_commands.standalone(hook.handler) is not None:
        return None
    verb = (setcmd.split_command(hook.handler) or ("",))[0]
    return msgs.ON_HANDLER_NEEDS_REPL.said(event=hook.event, command=verb)


def table(cfg: Config, paths: list[Path], *, reported: set | None = None) -> events.EventHooks:
    """The `on` handlers the jsrc files ``paths`` register for a run outside
    the REPL: those whose command stands alone (`Command.stands_alone`). An
    `on` line that registers no handler, one whose command needs the REPL
    among them, is reported on stderr: once per line when the runs that build
    a table each share ``reported``."""
    hooks = events.EventHooks(_standalone_dispatcher(cfg), admit=_admit_standalone)
    for path, lineno, line in setting_layers.jsrc_commands(paths):
        parsed = setcmd.split_command(line)
        if parsed is not None and parsed[0] == "on" and (error := setcmd.on_command(hooks, parsed[1]).error):
            if reported is not None:
                if (path, lineno) in reported:
                    continue
                reported.add((path, lineno))
            msgs.warn(msgs.SCRIPT_LINE_FAILED, path=path, lineno=lineno, error=error)
    return hooks
