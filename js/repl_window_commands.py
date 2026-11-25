"""The REPL's /window: ircII's window ops over the screen's windows
(`screen.Windows`) and the subagents in them (`subagent_windows`).

`/window` takes a chain of ops, run in order on the current window, as ircII
does: `/window new hide`, `/window 3 kill`. An op may be shortened to any
prefix that names one op, or that names one op every other match starts
with (`k` is kill, `h` is hide). A bare number or window name makes that
window current. With no ops it lists the windows."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from . import config as config_mod, messages as msgs
from .config import Config
from .repl_job_commands import _on_loop
from .repl_state import Command, ReplState

# What an op's argument is: a window (number or name), a whole number, a
# name, or an agent.
WINDOW, NUMBER, NAME, AGENT = "window", "number", "name", "agent"
_ARG_WORDS = {WINDOW: msgs.WINDOW_ARG_WINDOW, NUMBER: msgs.WINDOW_ARG_NUMBER,
              NAME: msgs.WINDOW_ARG_NAME, AGENT: msgs.WINDOW_ARG_AGENT}


@dataclass
class _Run:
    """What the ops of one /window line act on, and the rows `list` gathered."""

    windows: Any  # subagent_windows.SubagentWindows
    agent: str
    listed: list[list[dict]]

    @property
    def screen(self):
        return self.windows.windows


def _ref(run: _Run, word: str):
    view = run.screen.find(word)
    return view.number if view is not None else None


def _op_list(run: _Run, arg) -> None:
    run.listed.append(run.windows.rows())


def _op_new(run: _Run, arg):
    return run.windows.new(run.agent)


def _op_agent(run: _Run, agent: str):
    return run.windows.set_agent(run.screen.current, agent)


def _op_kill(run: _Run, arg):
    if run.screen.current == 1:
        return msgs.WINDOW_MAIN_STAYS.said()
    run.screen.kill(run.screen.current)
    return None


def _op_kill_others(run: _Run, arg) -> None:
    for number in run.screen.numbers():
        if number not in (1, run.screen.current):
            run.screen.kill(number)


def _op_hide(run: _Run, arg):
    if not run.screen.hide():
        return msgs.WINDOW_LAST_ON_SCREEN.said(number=run.screen.current)
    return None


def _op_hide_others(run: _Run, arg) -> None:
    run.screen.hide_others()


def _op_show(run: _Run, number: int):
    run.screen.show(number)


def _op_swap(run: _Run, number: int):
    run.screen.swap(number)


def _op_refnum(run: _Run, number: int):
    run.screen.switch(number)


def _op_goto(run: _Run, place: int):
    visible = run.screen.visible
    if not 1 <= place <= len(visible):
        return msgs.WINDOW_NOT_ON_SCREEN.said(number=place)
    run.screen.switch(visible[place - 1])
    return None


def _op_next(run: _Run, arg) -> None:
    run.screen.cycle(1)


def _op_previous(run: _Run, arg) -> None:
    run.screen.cycle(-1)


def _op_back(run: _Run, arg):
    previous = run.screen.previous
    if previous is None or not run.screen.switch(previous):
        return msgs.WINDOW_NO_BACK.said()
    return None


def _op_name(run: _Run, name: str):
    if name == "-":
        run.screen.view().name = ""
        return None
    if name.isdigit():
        return msgs.WINDOW_NAME_DIGITS.said(name=name)
    holder = run.screen.find(name)
    if holder is not None and holder.number != run.screen.current:
        return msgs.WINDOW_NAME_TAKEN.said(number=holder.number, name=name)
    run.screen.view().name = name
    run.screen.invalidate()
    return None


def _op_number(run: _Run, number: int):
    if not run.screen.renumber(number):
        return msgs.WINDOW_NUMBER_ONE.said()
    return None


def _op_move(run: _Run, step: int) -> None:
    run.screen.move(step)


def _op_grow(run: _Run, rows: int) -> None:
    run.screen.resize(by=rows)


def _op_shrink(run: _Run, rows: int) -> None:
    run.screen.resize(by=-rows)


def _op_size(run: _Run, rows: int) -> None:
    run.screen.resize(rows)


def _op_balance(run: _Run, arg) -> None:
    run.screen.balance()


# Each op: the argument it takes (None for none) and what it does.
OPS: dict[str, tuple[str | None, Callable[[_Run, Any], Any]]] = {
    "list": (None, _op_list),
    "new": (None, _op_new),
    "agent": (AGENT, _op_agent),
    "kill": (None, _op_kill),
    "kill_others": (None, _op_kill_others),
    "hide": (None, _op_hide),
    "hide_others": (None, _op_hide_others),
    "show": (WINDOW, _op_show),
    "swap": (WINDOW, _op_swap),
    "refnum": (WINDOW, _op_refnum),
    "goto": (NUMBER, _op_goto),
    "next": (None, _op_next),
    "previous": (None, _op_previous),
    "back": (None, _op_back),
    "name": (NAME, _op_name),
    "number": (NUMBER, _op_number),
    "move": (NUMBER, _op_move),
    "grow": (NUMBER, _op_grow),
    "shrink": (NUMBER, _op_shrink),
    "size": (NUMBER, _op_size),
    "balance": (None, _op_balance),
}


def match_op(word: str) -> tuple[str | None, list[str]]:
    """The op ``word`` names, and the ops it could be: an exact name, else
    the one prefix match that every other match starts with."""
    word = word.lower()
    if word in OPS:
        return word, [word]
    matches = [op for op in OPS if op.startswith(word)]
    shortest = min(matches, key=len) if matches else None
    if shortest is not None and all(op.startswith(shortest) for op in matches):
        return shortest, matches
    return None, matches


def _parse(words: list[str], screen) -> tuple[list[tuple[str, Any]], msgs.Said | None]:
    """The ops of a /window line with their arguments, or why it does not parse."""
    steps: list[tuple[str, Any]] = []
    i = 0
    while i < len(words):
        word = words[i]
        i += 1
        op, matches = (word.lower(), [word]) if word.lower() in OPS else (None, [])
        if op is None:
            view = screen.find(word)
            if view is not None or word.isdigit():
                steps.append(("refnum", word))
                continue
            op, matches = match_op(word)
        if op is None:
            if matches:
                return steps, msgs.WINDOW_AMBIGUOUS_OP.said(op=word, ops=", ".join(matches))
            return steps, msgs.WINDOW_UNKNOWN_OP.said(op=word, ops=" ".join(OPS))
        kind = OPS[op][0]
        if kind is None:
            steps.append((op, None))
            continue
        if i >= len(words):
            return steps, msgs.WINDOW_OP_NEEDS.said(op=op, what=_ARG_WORDS[kind].words())
        arg = words[i]
        i += 1
        if kind == NUMBER:
            try:
                arg = int(arg)
            except ValueError:
                return steps, msgs.WINDOW_OP_NEEDS.said(op=op, what=_ARG_WORDS[kind].words())
        elif kind == AGENT:
            try:
                config_mod.validate_agent_id(arg)
            except ValueError as exc:
                return steps, msgs.COMMAND_REFUSED.said(error=str(exc))
        steps.append((op, arg))
    return steps, None


def _apply(run: _Run, steps: list[tuple[str, Any]]) -> msgs.Said | None:
    """Run ``steps`` in order; the first refusal stops the rest."""
    for op, arg in steps:
        if OPS[op][0] == WINDOW or op == "refnum":
            number = _ref(run, arg)
            if number is None:
                return msgs.NO_SUCH_WINDOW.said(number=arg)
            arg = number
        refused = OPS[op][1](run, arg)
        if refused is not None:
            return refused
    return None


def _say_rows(rows: list[dict]) -> None:
    for row in rows:
        mark = (msgs.WINDOW_MARK_CURRENT.words() if row["current"]
                else msgs.WINDOW_MARK_ACTIVE.words() if row["active"] else "")
        msgs.say(msgs.WINDOW_ROW, number=row["number"], mark=mark, name=row["name"], agent=row["agent"],
                 state=row["state"], where=msgs.WINDOW_VISIBLE.words() if row["visible"] else "")


def _cmd_window(arg: str, state: ReplState, cfg: Config) -> msgs.Said | None:
    """`/window [op [arg]]...`: the ops in order (see `OPS`); bare, `list`."""
    windows = state.windows
    if windows is None:
        msgs.say(msgs.REPL_ONLY, command="/window")
        return None
    words = arg.split() or ["list"]
    steps, refused = _parse(words, windows.windows)
    if refused is not None:
        return refused
    agent = state.config_for_turn(cfg).agent_id if any(op == "new" for op, _ in steps) else ""
    run = _Run(windows, agent, [])
    refused = _on_loop(lambda: _apply(run, steps))
    for rows in run.listed:
        _say_rows(rows)
    return refused


COMMANDS: dict[str, Command] = {
    "window": Command(_cmd_window, "window [op [arg]]...", msgs.CMD_WINDOW),
}
