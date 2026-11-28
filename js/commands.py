"""The js commands: a first argument that names one runs that command, the
rest of the command line going to its module's `main(argv)`, which has its
own parser and its own `--help`. Any other first argument is the js command
line of `js.command_line`.

Each module is imported only when its command runs, so `js --help` lists
the commands without loading them."""
from __future__ import annotations

import importlib
from collections.abc import Callable

from . import messages as msgs

# name: (module under js, its line in `js --help`)
COMMANDS: dict[str, tuple[str, msgs.Message]] = {
    "generate": ("generate_cli", msgs.COMMAND_GENERATE),
    "init": ("init_cli", msgs.COMMAND_INIT),
    "rotate": ("rotate_cli", msgs.COMMAND_ROTATE),
}


def named(argv: list[str]) -> str | None:
    """The command `argv` runs: its first word when that names one."""
    return argv[0] if argv and argv[0] in COMMANDS else None


def entry(name: str) -> Callable[[list[str]], int]:
    """The `main(argv)` of command `name`."""
    return importlib.import_module(f"{__package__}.{COMMANDS[name][0]}").main


def listing() -> str:
    """The commands, one line each, as `js --help` and `--help-full` show them."""
    lines = [(name, summary.words()) for name, (_module, summary) in COMMANDS.items()]
    lines.append(("COMMAND --help", msgs.COMMANDS_HELP.words()))
    width = max(len(name) for name, _summary in lines)
    rows = [msgs.COMMANDS_ROW.words(name=name.ljust(width), summary=summary) for name, summary in lines]
    return "\n".join([msgs.COMMANDS_HEADING.words(), *rows])
