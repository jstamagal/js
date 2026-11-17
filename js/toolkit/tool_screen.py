"""How the screen shows a stock tool's call and result: the presentation each tool declared, looked up by name, and a result parsed into what the screen draws."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from typing import Any

from ..control_sequences import clean
from .discovery import discovery_tool
from .presentation import GENERIC, Presentation, listed_names
from .registry import build_default_registry
from .results import result_text


@cache
def _declarations() -> dict[str, Presentation]:
    # The discovery tool is bound to one turn's surface; this one is built
    # only for its declaration.
    tools = (*build_default_registry(()).tools, discovery_tool(None))
    return {tool.name: tool.presentation for tool in tools}


def declared(name: str) -> Presentation:
    """How the screen shows calls of the stock tool called `name`; for any
    other name, the generic way."""
    return _declarations().get(name, GENERIC)


_SHELL_HEADER = re.compile(r"shell=[^\n]*\nexit=(-?\d+)\n")


_SHELL_SECTION = re.compile(r"^--- (stdout|stderr) ---$")


# A `read` result line: `N|` before the file's own text.
_READ_GUTTER = re.compile(r"^(\d+)\|")


# The note a paged `read` ends with, written for the model.
_READ_PAGING = re.compile(r"^\[(\d+) total lines; continue with .*\]$")


_PATCHED = re.compile(r"^patched .*?\((.*), hash \w+\)$")


_HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def call_kind(name: str, args: dict | None, settings: Any = None) -> str:
    """What TOOLS_DISPLAY 1 shows of a call.

    quiet    one row, and a run of them folds into one row per tool
    command  its output while it runs, one row once it ends
    diff     the change it made
    agent    a subagent run: a row when it starts, its result when it ends
    loud     its result
    """
    args = args if isinstance(args, dict) else {}
    if name in listed_names(settings, "TOOLS_QUIET"):
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
