"""Tool-specific screen metadata and adapters for legacy text results."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .. import settings as _settings


# The argument each tool carries its program or file body in, shown as a
# highlighted block under the call header instead of on it.
_BODY_ARG: dict[str, str] = {
    "shell": "command",
    "terminal_session": "command",
    "kernel": "code",
    "toolbox": "source",
    "write": "content",
    "plan": "content",
}
# Arguments the header leaves out: the body, or text the result's diff shows.
_UNLISTED_ARGS: dict[str, tuple[str, ...]] = {
    "patch": ("old_string", "new_string", "edits"),
    "read": ("range",),
    "shell": ("description",),
    "send": ("text",),
    "post_task": ("body",),
    "finish_task": ("result",),
}
_SHELL_HEADER = re.compile(r"shell=[^\n]*\nexit=(-?\d+)\n")
_SHELL_SECTION = re.compile(r"^--- (stdout|stderr) ---$")
_PYTHON_FIRST_LINE = re.compile(r"^\s*(?:\S*/)?python[0-9.]*\b")
# A `read` result line: `N|` before the file's own text.
_READ_GUTTER = re.compile(r"^(\d+)\|")
# The note a paged `read` ends with, written for the model.
_READ_PAGING = re.compile(r"^\[(\d+) total lines; continue with .*\]$")
_PATCHED = re.compile(r"^patched .*?\((.*), hash \w+\)$")
_HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


# The argument that tells one call of a tool from another on its header: the
# first of these that the call carries. A tool not named here is told apart by
# its path, when it has one.
_KEY_ARGS: dict[str, tuple[str, ...]] = {
    "fs_search": ("pattern",),
    "ast_search": ("pattern",),
    "shell": ("command",),
    "terminal_session": ("command",),
    "kernel": ("code",),
    "toolbox": ("source",),
    "fetch": ("url",),
    "browse": ("url",),
    "exa_search": ("query",),
    "serper_search": ("query",),
    "tavily_search": ("query",),
    "docs_search": ("topic",),
    "tool_discovery": ("query",),
    "skill": ("name",),
    "send": ("to",),
    "claim": ("key",),
    "release": ("key",),
    "plan": ("plan_name",),
    "post_task": ("title",),
    "task": ("agent_id",),
}
_PATH_ARGS = ("file_path", "path")


# The argument a call's own text is in, shown at TOOLS_DISPLAY 1 in place of the
# result, which only acknowledges it.
_SAID_ARG: dict[str, str] = {"send": "text", "post_task": "body", "finish_task": "result"}
# Rows of a running command's output kept on screen.


def builtin_lexer(name: str, args: dict, body: str) -> str | None:
    if name in ("kernel", "toolbox"):
        return "python"
    if name in ("shell", "terminal_session"):
        first = body.lstrip().split("\n", 1)[0]
        return "python" if _PYTHON_FIRST_LINE.match(first) else "bash"
    if name == "plan":
        return "markdown"
    return None



def _names(settings: Any, key: str) -> frozenset[str]:
    value = _settings.knob(settings, key)
    return frozenset(str(item) for item in value) if isinstance(value, (list, tuple)) else frozenset()


_SHELL_JOIN = re.compile(r"\s*(?:&&|\|\||;|\|)\s*")


def _looks_around(command: Any, settings: Any) -> bool:
    """A shell command made only of SHELL_QUIET commands, writing nowhere."""
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
    loud     its result
    """
    args = args if isinstance(args, dict) else {}
    if name in _names(settings, "TOOLS_QUIET"):
        return "quiet"
    if name == "shell":
        return "quiet" if _looks_around(args.get("command"), settings) else "command"
    if name in ("kernel", "terminal_session", "toolbox"):
        return "command"
    if name in ("patch", "write", "notebook_edit"):
        return "diff"
    return "loud"


@dataclass
class ToolOutput:
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


def result_text(result: Any) -> tuple[str, bool]:
    """Flatten a tool result to (text, is_error)."""
    dehydrated = getattr(result, "dehydrated", None)
    if callable(dehydrated):
        return dehydrated(), bool(getattr(result, "is_error", False))
    text = result if isinstance(result, str) else str(result)
    return text, text.lstrip().startswith("ERROR")


# The count `runtime.ToolErrorTracker` appends to a failed result for the model.
_RETRY_NOTE = re.compile(r"\s*<retry>attempts_left=(\d+), allowed_max_attempts=(\d+)</retry>\s*$")


def tool_output(name: str, result: Any) -> ToolOutput:
    """Parse a result for display. A finished shell result loses its
    `shell=… exit=… --- stdout ---` envelope; exit is kept apart. A failed
    call's retry note leaves the text and becomes `tries`."""
    from ..display import clean

    if isinstance(result, ToolOutput):
        return result
    text, is_error = result_text(result)
    text = clean(text)
    tries: tuple[int, int] | None = None
    note = _RETRY_NOTE.search(text)
    if note is not None:
        tries = (int(note.group(1)), int(note.group(2)))
        text = text[:note.start()]
    if name == "shell":
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
            return ToolOutput(lines, exit_code=exit_code or None, is_error=is_error)
    body = text.rstrip("\n")
    output = ToolOutput([(line, False) for line in body.split("\n")] if body else [],
                        is_error=is_error, tries=tries)
    if name == "read":
        output.read = read_data(output)
    elif name == "patch":
        output.diff = diff_data(output)
    return output


def read_data(output: ToolOutput) -> tuple[list[tuple[str, str]], int | None] | None:
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



def diff_data(output: ToolOutput) -> tuple[list[tuple[str, str, str]], str] | None:
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
