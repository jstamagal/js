"""Resolve --home before CLI startup reads or migrates a profile."""
from __future__ import annotations

from . import paths


def configure_home(argv: list[str]) -> list[str]:
    """Select the profile and return arguments with --home removed for dispatch."""
    selected = None
    remaining: list[str] = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == '--':
            remaining.extend(argv[index:])
            break
        if arg == '--home':
            index += 1
            if index >= len(argv) or argv[index].startswith('-'):
                raise ValueError('--home requires a directory')
            selected = argv[index]
        elif arg.startswith('--home='):
            selected = arg.partition('=')[2]
            if not selected:
                raise ValueError('--home requires a directory')
        else:
            remaining.append(arg)
        index += 1
    paths.configure_home(selected)
    return remaining
