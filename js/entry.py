"""Where the `js` command starts. A bare `js --help` prints the usage from
`js.command_line` alone; every other command line goes to `js.cli`."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    given = sys.argv[1:] if argv is None else argv
    if given in (["-h"], ["--help"]):
        from . import command_line

        command_line.parser().parse_args(given)
    from .cli import main as run

    return run(argv)
