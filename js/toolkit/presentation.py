"""How the screen shows a tool: the `Presentation` each tool declares, and
its result parsed into what the screen draws."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
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


_GENERIC = Presentation()


@cache
def _declarations() -> dict[str, Presentation]:
    from .discovery import discovery_tool
    from .registry import build_default_registry

    # The discovery tool is bound to one turn's surface; this one is built
    # only for its declaration.
    tools = (*build_default_registry(()).tools, discovery_tool(None))
    return {tool.name: tool.presentation for tool in tools}


def declared(name: str) -> Presentation:
    """How the screen shows calls of the stock tool called `name`; for any
    other name, the generic way."""
    return _declarations().get(name, _GENERIC)


_SHELL_HEADER = re.compile(r"shell=[^\n]*\nexit=(-?\d+)\n")
_SHELL_SECTION = re.compile(r"^--- (stdout|stderr) ---$")
# A `read` result line: `N|` before the file's own text.
_READ_GUTTER = re.compile(r"^(\d+)\|")
# The note a paged `read` ends with, written for the model.
_READ_PAGING = re.compile(r"^\[(\d+) total lines; continue with .*\]$")
_PATCHED = re.compile(r"^patched .*?\((.*), hash \w+\)$")
_HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _names(settings: Any, key: str) -> frozenset[str]:
    value = _settings.knob(settings, key)
    return frozenset(str(item) for item in value) if isinstance(value, (list, tuple)) else frozenset()


_SHELL_JOIN = re.compile(r"\s*(?:&&|\|\||;|\|)\s*")


def looks_around(args: dict, settings: Any) -> bool:
    """A call whose shell command is made only of SHELL_QUIET commands,
    writing nowhere."""
    command = args.get("command")
    if not isinstance(command, str) or not command.strip() or "\n" in command.strip() or ">" in command:
        return False
    quiet = _names(settings, "SHELL_QUIET")
    for part in _SHELL_JOIN.split(command.strip()):
        words = part.split()
        if not words or (words[0] not in quiet and " ".join(words[:2]) not in quiet):
            return False
    return True


def call_kind(name: str, args: dict | None, settings: Any = None) -> str:
    """What TOOLS_DISPLAY 1 shows of a call.

    quiet    one row, and a run of them folds into one row per tool
    command  its output while it runs, one row once it ends
    diff     the change it made
    agent    a subagent run: a row when it starts, its result when it ends
    loud     its result
    """
    args = args if isinstance(args, dict) else {}
    if name in _names(settings, "TOOLS_QUIET"):
        return "quiet"
    if isinstance(args.get("tasks"), list):
        return "agent"
    shown = declared(name)
    if shown.quiet_when is not None and shown.quiet_when(args, settings):
        return "quiet"
    return shown.kind


@dataclass
class ScreenResult:
    """A tool result as the screen shows it: cleaned lines with their stream."""

    lines: list[tuple[str, bool]]  # (text, is_stderr)
    exit_code: int | None = None
    is_error: bool = False
    tries: tuple[int, int] | None = None  # (left, allowed) of a failed call

    read: tuple[list[tuple[str, str]], int | None] | None = None
    diff: tuple[list[tuple[str, str, str]], str] | None = None

    @property
    def text(self) -> str:
        return "\n".join(line for line, _ in self.lines)

    @property
    def failed(self) -> bool:
        return self.is_error or bool(self.exit_code)


# The count `tool_errors.ToolErrorTracker` appends to a failed result for the model.
_RETRY_NOTE = re.compile(r"\s*<retry>attempts_left=(\d+), allowed_max_attempts=(\d+)</retry>\s*$")


def tool_output(name: str, result: Any) -> ScreenResult:
    """Parse a result for display. A command's result loses its
    `shell=… exit=… --- stdout ---` envelope; exit is kept apart. A failed
    call's retry note leaves the text and becomes `tries`."""
    from ..control_sequences import clean
    from .results import result_text

    if isinstance(result, ScreenResult):
        return result
    text, is_error = result_text(result, screen=True)
    text = clean(text)
    tries: tuple[int, int] | None = None
    note = _RETRY_NOTE.search(text)
    if note is not None:
        tries = (int(note.group(1)), int(note.group(2)))
        text = text[:note.start()]
    reads = declared(name).result
    if reads == "command":
        header = _SHELL_HEADER.match(text)
        if header is not None:
            exit_code = int(header.group(1))
            lines: list[tuple[str, bool]] = []
            stream: str | None = None
            for line in text.split("\n"):
                section = _SHELL_SECTION.match(line)
                if section is not None:
                    stream = section.group(1)
                    continue
                if stream is not None:
                    lines.append((line, stream == "stderr"))
            while lines and not lines[-1][0]:
                lines.pop()
            return ScreenResult(lines, exit_code=exit_code or None, is_error=is_error)
    body = text.rstrip("\n")
    output = ScreenResult([(line, False) for line in body.split("\n")] if body else [],
                        is_error=is_error, tries=tries)
    if reads == "file":
        output.read = read_data(output)
    elif reads == "diff":
        output.diff = diff_data(output)
    return output


def read_data(output: ScreenResult) -> tuple[list[tuple[str, str]], int | None] | None:
    """A `read` result as numbered file rows and the file's total line count
    from the paging note; None when the result has no `N|` lines."""
    matches = [_READ_GUTTER.match(line) for line, _ in output.lines]
    if not any(matches):
        return None
    total: int | None = None
    kept: list[tuple[str, str]] = []
    notes: list[str] = []
    for (line, _), match in zip(output.lines, matches, strict=True):
        paging = _READ_PAGING.match(line)
        if match is not None:
            kept.append((match.group(1), line[match.end():]))
        elif paging is not None:
            total = int(paging.group(1))
        elif line.strip():
            notes.append(line)
    return kept + [("", note) for note in notes], total



def diff_data(output: ScreenResult) -> tuple[list[tuple[str, str, str]], str] | None:
    """A `patch` result as numbered diff rows and its summary; None when the
    result is not `patched … (summary, hash …)` followed by a unified diff."""
    if not output.lines:
        return None
    summary = _PATCHED.match(output.lines[0][0])
    if summary is None:
        return None
    rows: list[tuple[str, str, str]] = []
    old = new = 0
    for line, _ in output.lines[1:]:
        hunk = _HUNK.match(line)
        if hunk is not None:
            if rows:
                rows.append(("", "", "gap"))
            old, new = int(hunk.group(1)), int(hunk.group(2))
        elif line.startswith(("--- ", "+++ ")) and not old and not new:
            continue
        elif line.startswith("+"):
            rows.append((str(new), "+ " + line[1:], "added"))
            new += 1
        elif line.startswith("-"):
            rows.append((str(old), "- " + line[1:], "removed"))
            old += 1
        elif line.startswith(" "):
            rows.append((str(new), "  " + line[1:], "panel_text"))
            old, new = old + 1, new + 1
    return rows, summary.group(1)
