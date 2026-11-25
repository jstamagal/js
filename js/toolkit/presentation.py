"""How the screen shows a tool: the `Presentation` each tool declares, and
its result parsed into what the screen draws."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .. import settings as _settings

PATH_ARGS = ("file_path", "path")
_PYTHON_FIRST_LINE = re.compile(r"^\s*(?:\S*/)?python[0-9.]*\b")


@dataclass(frozen=True)
class Presentation:
    """How the screen shows one tool's calls and results, declared on its
    `Tool` row. The default is how a tool the screen knows nothing about is
    shown: its path on the header, every other argument listed, its result
    as text."""

    # The arguments that tell one call from another on the header: the first
    # of these that the call carries.
    key: tuple[str, ...] = PATH_ARGS
    # The argument the call carries its program or file body in, shown as a
    # highlighted block under the header instead of on it.
    body: str | None = None
    # The body's lexer; "bash" is read as "python" when its first line runs
    # python. None guesses from the call's path.
    lexer: str | None = None
    # The body is a file's text: numbered rows, even when it is one line.
    body_is_file: bool = False
    # Arguments the header leaves out below TOOLS_DISPLAY 3: text the result's
    # diff or the header's note shows.
    unlisted: tuple[str, ...] = ()
    # The argument holding what the call says: left off the header and shown
    # at TOOLS_DISPLAY 1 in place of the result, which only acknowledges it.
    said: str | None = None
    # The dim note the header opens its other arguments with: "range", the
    # line or byte range read; "lines", the body's line count; "edits", how
    # many `edits` the call carries.
    note: str | None = None
    # What TOOLS_DISPLAY 1 shows of a call (`call_kind`): "loud", "command"
    # or "diff".
    kind: str = "loud"
    # True when a call with these arguments, under these settings, is quiet.
    quiet_when: Callable[[dict, Any], bool] | None = None
    # How the result reads: "text"; "command", a `shell=… exit=…` envelope
    # around stdout and stderr; "file", a `read`'s numbered `N|` rows;
    # "diff", a `patched …` summary over a unified diff.
    result: str = "text"
    # What a fold row counts, a key of `messages.TOOL_FOLD_NOUNS`.
    fold_noun: str = "call"
    # A fold row names a call by its command's program rather than by the
    # head of its key argument.
    fold_by_program: bool = False
    # The tail of its output shows while it runs.
    live: bool = False

    def language(self, body: str) -> str | None:
        """The lexer of the call's `body`; None when the path should tell."""
        if self.lexer == "bash" and _PYTHON_FIRST_LINE.match(body.lstrip().split("\n", 1)[0]):
            return "python"
        return self.lexer


GENERIC = Presentation()


def listed_names(settings: Any, key: str) -> frozenset[str]:
    value = _settings.knob(settings, key)
    return frozenset(str(item) for item in value) if isinstance(value, (list, tuple)) else frozenset()


_SHELL_JOIN = re.compile(r"\s*(?:&&|\|\||;|\|)\s*")


def looks_around(args: dict, settings: Any) -> bool:
    """A call whose shell command is made only of SHELL_QUIET commands,
    writing nowhere."""
    command = args.get("command")
    if not isinstance(command, str) or not command.strip() or "\n" in command.strip() or ">" in command:
        return False
    quiet = listed_names(settings, "SHELL_QUIET")
    for part in _SHELL_JOIN.split(command.strip()):
        words = part.split()
        if not words or (words[0] not in quiet and " ".join(words[:2]) not in quiet):
            return False
    return True


