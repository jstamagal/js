"""Terminal presentation: model text and tool exchanges on their way to a screen.

Every byte of model or tool output passes `clean()` before a terminal sees it,
so a model that cats a binary cannot leave the terminal needing `reset`.

`Display` owns one stream of answer text. On a terminal it renders Markdown:
finished top-level blocks commit once and are never redrawn; only the last,
still-open block is live. Anywhere else it writes the cleaned text as it
arrives.

The tool renderers build the exchange a turn prints at TOOLS_DISPLAY 0-3: a
header (mark, tool name, key argument, the other arguments dim), the program
or output in a panel, a file or a diff as a numbered view, and a dim footer
with the metrics. The colours are /fset's `COLOR_*` entries (`js.fset`).
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import shutil
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from rich.cells import cell_len
from rich.console import Console
from rich.markdown import Markdown
from rich.syntax import Syntax
from rich.text import Text
from rich.theme import Theme as _RichTheme

from . import fset
from . import messages as msgs
from . import settings as _settings

# String sequences (OSC, DCS, SOS, PM, APC) run to BEL or ST, 7- or 8-bit.
_STRING_SEQ = re.compile(r"(?:\x1b[\]PX^_]|[\x90\x98\x9d\x9e\x9f]).*?(?:\x07|\x1b\\|\x9c)", re.DOTALL)
_CSI = re.compile(r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]")
# Any other escape: ESC, intermediates, one final byte (ESC c, ESC ( 0, ESC 7).
_ESC = re.compile(r"\x1b[ -/]*[0-~]")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_STRING_OPEN = re.compile(r"\x1b[\]PX^_]|[\x90\x98\x9d\x9e\x9f]")
_STRING_CLOSE = re.compile(r"\x07|\x1b\\|\x9c")


def _strip_string_sequences(text: str) -> str:
    """``_STRING_SEQ.sub("", text)`` in linear time. The lazy regex rescans to
    the end of the text from every unterminated opener: quadratic, 2.4 s for
    40 KB of ``\\x1b]``, re-run on the whole live answer every refresh. Here the
    first opener with no terminator after it ends the scan, because no later
    opener can have one either; the text from it on is kept for the later
    passes to scrub, as before."""
    out: list[str] = []
    pos = 0
    while True:
        opened = _STRING_OPEN.search(text, pos)
        if opened is None:
            break
        closed = _STRING_CLOSE.search(text, opened.end())
        if closed is None:
            break
        out.append(text[pos:opened.start()])
        pos = closed.end()
    out.append(text[pos:])
    return "".join(out)


def clean(text: str) -> str:
    """Model and tool output as text, never as terminal control sequences.

    Removes OSC/DCS-style strings, CSI sequences and other escapes, then every
    C0/C1 control byte except newline and tab."""
    text = _strip_string_sequences(text)
    text = _CSI.sub("", text)
    text = _ESC.sub("", text)
    return _CONTROL.sub("", text)


# Transcript marker for a tool exchange: typeable ASCII, one per exchange, so a
# grep of a saved transcript finds every exchange the screen showed.
TOOL_MARKER = ">"

def chrome(text: str) -> str:
    """Harness-subroutine output: bold COLOR_TEXT on COLOR_PANEL."""
    return f"{fset.BOLD}{fset.fg('COLOR_TEXT')}{fset.bg('COLOR_PANEL')}{text}{fset.RESET}"


# The two voices of a conversation. The user's line sits behind the input
# prompt in bold; the answer opens with a mirrored glyph on its own line.
def prompt() -> str:
    """The input prompt, in COLOR_USER_MARK."""
    return f"{fset.BOLD}{fset.paint('COLOR_USER_MARK', msgs.INPUT_PROMPT)}"


def assistant_mark() -> str:
    """The mark an answer opens with, in COLOR_AGENT_MARK."""
    return fset.paint("COLOR_AGENT_MARK", "❮") + "\n"


def user_line(text: str) -> str:
    """One user message as the conversation shows it."""
    return f"{prompt()}{fset.BOLD}{fset.paint('COLOR_USER', text)}{fset.RESET}\n"


def echo_user(text: str) -> None:
    """Print `user_line(text)` where the answer will follow."""
    sys.stdout.write(user_line(text))
    sys.stdout.flush()


def _setting(settings: Any, key: str) -> Any:
    """Knob ``key`` from live settings; its js/jsrc value when absent."""
    return _settings.knob(settings if isinstance(settings, dict) else None, key)


def tools_level(settings: Any) -> int:
    """TOOLS_DISPLAY from live settings: 0 nothing, 1 what matters, 2 preview, 3 whole."""
    level = _setting(settings, "TOOLS_DISPLAY")
    return level if isinstance(level, int) and level in range(4) else _settings.default_value("TOOLS_DISPLAY")


def preview_lines(settings: Any) -> int:
    lines = _setting(settings, "TOOLS_PREVIEW_LINES")
    return lines if isinstance(lines, int) and lines > 0 else _settings.default_value("TOOLS_PREVIEW_LINES")


def markdown_enabled(settings: Any) -> bool:
    return _setting(settings, "MARKDOWN") is not False


def terminal_width() -> int:
    return max(20, shutil.get_terminal_size((80, 24)).columns)


def block_width() -> int:
    """Columns a drawn block may fill: one short of the terminal. The async
    screen's scrollback keeps a cursor cell after every line, so a row as wide
    as the window wraps onto a blank second row."""
    return terminal_width() - 1


def _render_console(width: int, styles: dict[str, str] | None = None) -> Console:
    return Console(
        theme=_RichTheme(styles) if styles else None,
        file=io.StringIO(),
        force_terminal=True,
        color_system="truecolor",
        width=width,
        highlight=False,
        markup=False,
        emoji=False,
        soft_wrap=False,
    )


def _rendered_text(console: Console) -> str:
    out = console.file.getvalue()  # type: ignore[attr-defined]
    lines = [line.rstrip(" ") for line in out.split("\n")]
    return "\n".join(lines)


def _markdown_styles(colours: Theme) -> dict[str, str]:
    """Rich's Markdown styles that carry a colour, in the theme's colours."""
    return {
        "markdown.code": f"{colours.code} on {colours.panel}",
        "markdown.code_block": colours.panel_text,
        "markdown.block_quote": colours.dim,
        "markdown.list": colours.highlight,
        "markdown.item.number": colours.highlight,
        "markdown.h2": f"underline {colours.highlight}",
        "markdown.h3": f"bold {colours.highlight}",
        "markdown.h4": f"italic {colours.highlight}",
        "markdown.link": colours.highlight,
        "markdown.link_url": f"underline {colours.dim}",
        "markdown.table.border": colours.separator,
        "markdown.table.header": f"not bold {colours.highlight}",
        "markdown.kbd": f"bold {colours.highlight}",
    }


def render_markdown(source: str, width: int, settings: Any = None) -> str:
    """ANSI rendering of Markdown source at `width` columns: no blank lines
    before or after, ending in one newline. Inline code is a chip: its text in
    COLOR_CODE on COLOR_PANEL, a space of padding each side."""
    console = _render_console(width, _markdown_styles(theme(settings)))
    document = Markdown(source, code_theme=fset.code_style(), hyperlinks=False)
    for token in document.parsed:
        for child in token.children or ():
            if child.type == "code_inline":
                child.content = f" {child.content} "
    console.print(document)
    return _rendered_text(console).strip("\n") + "\n"


def _block_starts(source: str) -> list[int]:
    """Source line numbers where each top-level Markdown block begins."""
    return [token.map[0] for token in Markdown(source).parsed if token.level == 0 and token.map]


# --------------------------------------------------------------------------
# Answer text
# --------------------------------------------------------------------------


class LiveSurface(Protocol):
    """Where a pretty Display draws. `update` replaces the live region; `commit`
    replaces it with final text and closes it, so the next `update` starts a new
    region below."""

    def width(self) -> int: ...
    def update(self, rendered: str) -> None: ...
    def commit(self, rendered: str) -> None: ...


class TtyLive:
    """Live region on a plain terminal: the tail of the open block, redrawn in
    place with cursor-up and erase-below. Committed text is written once.

    Redraws and erases go through `write`; committed text goes through
    `commit_write` (default `write`), so a stream that logs what it writes can
    keep the final answer and skip the redraws."""

    def __init__(
        self,
        write: Callable[[str], None],
        flush: Callable[[], None] | None = None,
        *,
        commit_write: Callable[[str], None] | None = None,
    ) -> None:
        self._write = write
        self._commit_write = commit_write or write
        self._flush = flush or (lambda: None)
        self._rows = 0

    def width(self) -> int:
        # One column short: a line that fills the last column leaves the cursor
        # in the pending-wrap state and the row count would be off by one.
        return terminal_width() - 1

    def _erase(self) -> None:
        if self._rows:
            self._write(f"\033[{self._rows}F\033[J")
            self._rows = 0

    def update(self, rendered: str) -> None:
        self._erase()
        height = shutil.get_terminal_size((80, 24)).lines
        tail = rendered.rstrip("\n").split("\n")[-max(1, height - 2):]
        self._write("\n".join(tail) + "\n")
        self._rows = len(tail)
        self._flush()

    def commit(self, rendered: str) -> None:
        self._erase()
        self._commit_write(rendered)
        self._rows = 0
        self._flush()


class Display:
    """One stream of answer text.

    `chunk(field, text)` feeds it and `finish()` ends it. With a live surface it
    renders Markdown: each finished top-level block is rendered and committed
    once; the last block stays live until the next block starts or `finish()`.
    Redraws are at most one per `refresh_s`; text that arrives between two is
    drawn when the interval ends, whether or not more text follows.
    Without one it writes `clean(text)` as it arrives, no cursor movement.
    """

    def __init__(
        self,
        write: Callable[[str], None],
        *,
        live: LiveSurface | None = None,
        flush: Callable[[], None] | None = None,
        refresh_s: float = 0.1,
    ) -> None:
        self._write = write
        self._flush = flush or (lambda: None)
        self.live = live
        self.refresh_s = refresh_s
        self.field: str | None = None
        self._source = ""
        self._last_char = "\n"
        self._wrote = False
        self._refreshed = 0.0
        self._trailing: asyncio.TimerHandle | None = None
        self._committed_any = False

    @classmethod
    def for_stream(cls, stream: Any, *, markdown: bool = True) -> Display:
        redraw = getattr(stream, "write_unlogged", None) or stream.write
        flush = getattr(stream, "flush", None)
        try:
            tty = bool(stream.isatty())
        except Exception:  # noqa: BLE001 - a closed or foreign stream is not a terminal
            tty = False
        live = TtyLive(redraw, flush, commit_write=stream.write) if (markdown and tty) else None
        return cls(stream.write, live=live, flush=flush)

    @property
    def pretty(self) -> bool:
        return self.live is not None

    def mark(self, text: str) -> None:
        """Write `text` ahead of the stream, outside its Markdown."""
        if self.live is not None:
            self.live.commit(text)
        else:
            self._write(text)
        self._flush()

    def chunk(self, field: str, text: str) -> None:
        if not text:
            return
        if field != self.field:
            self.finish()
            self.field = field
        if self.live is None:
            cleaned = clean(text)
            if cleaned:
                self._write(cleaned)
                self._last_char = cleaned[-1]
                self._wrote = True
                self._flush()
            return
        self._source += text
        wait = self._refreshed + self.refresh_s - time.monotonic()
        if wait <= 0:
            self._cancel_trailing()
            self._refresh()
            self._refreshed = time.monotonic()
        elif self._trailing is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            self._trailing = loop.call_later(wait, self._draw_held_back)

    def _draw_held_back(self) -> None:
        """Draw the text the last redraw did not include. The model may go on
        to stream a tool call for minutes with no further text chunk. Commits
        nothing: a finished block is committed from `chunk` or `finish`, which
        the caller has wrapped."""
        self._trailing = None
        if self.live is not None:
            self._draw_live(clean(self._source))
            self._refreshed = time.monotonic()

    def _cancel_trailing(self) -> None:
        if self._trailing is not None:
            self._trailing.cancel()
            self._trailing = None

    def _commit(self, source: str) -> None:
        assert self.live is not None
        rendered = render_markdown(source, self.live.width())
        if self._committed_any:
            rendered = "\n" + rendered
        self.live.commit(rendered)
        self._committed_any = True

    def _refresh(self) -> None:
        assert self.live is not None
        value = clean(self._source)
        starts = _block_starts(value)
        if len(starts) > 1:
            # markdown-it numbers lines by "\n" only; splitlines() would also
            # split on U+2028/U+2029, which clean() keeps, and cut mid-block.
            lines = value.split("\n")
            cut = starts[-1]
            self._commit("\n".join(lines[:cut]) + "\n")
            value = "\n".join(lines[cut:])
            self._source = value
        self._draw_live(value)

    def _draw_live(self, value: str) -> None:
        assert self.live is not None
        if value.strip():
            separator = "\n" if self._committed_any else ""
            self.live.update(separator + render_markdown(value, self.live.width()))

    def finish(self) -> None:
        """Commit whatever is open and end the stream on a fresh line."""
        self._cancel_trailing()
        if self.live is not None:
            value = clean(self._source)
            if value.strip():
                self._commit(value)
            self._source = ""
            self._committed_any = False
        elif self._wrote:
            if self._last_char != "\n":
                self._write("\n")
            self._flush()
        self._wrote = False
        self._last_char = "\n"
        self.field = None


def print_answer(text: str, stream: Any, *, markdown: bool = True) -> None:
    """Write one finished answer: rendered Markdown on a terminal, else the
    cleaned text and a newline."""
    display = Display.for_stream(stream, markdown=markdown)
    display.chunk("text", text)
    display.finish()


# --------------------------------------------------------------------------
# Tool exchanges
# --------------------------------------------------------------------------

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
_ARG_VALUE_CHARS = 40
_SHELL_HEADER = re.compile(r"shell=[^\n]*\nexit=(-?\d+)\n")
_SHELL_SECTION = re.compile(r"^--- (stdout|stderr) ---$")
_PYTHON_FIRST_LINE = re.compile(r"^\s*(?:\S*/)?python[0-9.]*\b")
# A `read` result line: `N|` before the file's own text.
_READ_GUTTER = re.compile(r"^(\d+)\|")
# The note a paged `read` ends with, written for the model.
_READ_PAGING = re.compile(r"^\[(\d+) total lines; continue with .*\]$")
_PATCHED = re.compile(r"^patched .*?\((.*), hash \w+\)$")
_HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")

# Screen marks of an exchange. `MARK` opens a call that has not finished;
# a finished one is `OK` or `FAIL`.
MARK = "…"
OK = "✓"
FAIL = "✗"
# The bar down the left edge of an exchange, in the colour of how it stands.
RAIL = "▌"
_RAIL = 2
_SGR = re.compile(r"\x1b\[[0-9;]*m")
_RAILED = re.compile(rf"^{RAIL} ?", re.M)
_HEADER_MARK = re.compile(rf"^[{MARK}{OK}{FAIL}] +", re.M)  # the mark and the chip's padding
# Columns a body sits in from the rail, and a panel's inner padding.
_INDENT = 1
_PAD = 1


@dataclass(frozen=True)
class Theme:
    """The colours of a tool exchange and an answer, each an /fset colour entry."""

    accent: str      # tool name, running mark
    highlight: str   # Markdown headings, links, list numbers
    ok: str          # finished mark
    error: str       # failed mark, error text
    warn: str        # stderr
    dim: str         # arguments, line numbers, metrics
    panel: str       # output panel background
    panel_text: str  # output panel text
    added: str       # diff: added line
    removed: str     # diff: removed line
    code: str        # inline code in an answer
    separator: str   # rules and table borders


_THEME_ROLES = {
    "accent": "COLOR_TOOL", "highlight": "COLOR_ACCENT", "ok": "COLOR_OK", "error": "COLOR_ERROR", "warn": "COLOR_WARN",
    "dim": "COLOR_DIM", "panel": "COLOR_PANEL", "panel_text": "COLOR_TOOL_OUTPUT",
    "added": "COLOR_ADDED", "removed": "COLOR_REMOVED", "code": "COLOR_CODE", "separator": "COLOR_SEPARATOR",
}


def theme(settings: Any = None) -> Theme:
    """The colours /fset gives now; ``settings`` is unused, kept for callers."""
    return Theme(**{field: fset.hex_for(role) or "default" for field, role in _THEME_ROLES.items()})


def for_transcript(rendered: str) -> str:
    """`rendered` as the transcript log records it: plain text without the
    rail, every exchange header opening with `TOOL_MARKER` whatever mark the
    screen drew."""
    return _HEADER_MARK.sub(f"{TOOL_MARKER} ", _RAILED.sub("", _SGR.sub("", rendered)))


@dataclass
class ToolOutput:
    """A tool result as the screen shows it: cleaned lines with their stream."""

    lines: list[tuple[str, bool]]  # (text, is_stderr)
    exit_code: int | None = None
    is_error: bool = False
    tries: tuple[int, int] | None = None  # (left, allowed) of a failed call

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
    return ToolOutput([(line, False) for line in body.split("\n")] if body else [],
                      is_error=is_error, tries=tries)


def _byte_len(text: str) -> int:
    return len(text.encode("utf-8", errors="replace"))


def _size(count: int) -> str:
    if count < 1024:
        return f"{count} B"
    if count < 1024 * 1024:
        return f"{count / 1024:.1f} KB"
    return f"{count / (1024 * 1024):.1f} MB"


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def metrics(output: ToolOutput, *, shown: int | None = None, lines: int | None = None,
            of: int | None = None) -> str:
    """`[failed · L of N tries left · ][exit N · ]{lines} lines[ of {of}] · {size}`,
    plain text. `shown` is how many of the lines are on screen when some are
    hidden; `lines` overrides the result's own line count and `of` is the
    file's, for a `read` of one page."""
    parts: list[str] = []
    if output.tries is not None:
        parts.append(f"failed · {output.tries[0]} of {output.tries[1]} tries left")
    if output.exit_code:
        parts.append(f"exit {output.exit_code}")
    total = len(output.lines) if lines is None else lines
    if of is not None and of > total:
        parts.append(f"{total} of {_count(of, 'line')}")
    else:
        parts.append(_count(total, "line"))
    if shown is not None and shown < total:
        parts.append(f"{shown} shown")
    parts.append(_size(_byte_len(output.text)))
    return " · ".join(parts)


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
_KEY_ARG_MIN = 16


def short_path(path: str) -> str:
    """`path` under the working directory as a relative path, under the home
    directory as `~/…`, anything else unchanged."""
    for base, prefix in ((os.getcwd(), ""), (os.path.expanduser("~"), "~/")):
        if base not in ("", "/") and path.startswith(base.rstrip("/") + "/"):
            return prefix + path[len(base.rstrip("/")) + 1:]
    return path


def _key_name(name: str, args: dict | None) -> str | None:
    if not isinstance(args, dict):
        return None
    keys = _KEY_ARGS.get(name, _PATH_ARGS)
    return next((key for key in keys if isinstance(args.get(key), str) and args[key].strip()), None)


def key_argument(name: str, args: dict | None, room: int) -> str:
    """The call's key argument on one line of at most `room` cells: a path
    is shortened and keeps its tail, anything else its head; "" when the call
    has none."""
    key = _key_name(name, args)
    if key is None:
        return ""
    lines = clean(args[key]).strip().split("\n")  # type: ignore[index]
    text = lines[0].strip()
    more = "…" if len(lines) > 1 else ""
    if key in _PATH_ARGS:
        text = short_path(text)
    if len(text) + len(more) <= room:
        return text + more
    if key in _PATH_ARGS:
        return "…" + text[-(room - 1):]
    return text[:room - 1] + "…"


def _single_line(value: Any) -> str:
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(value)
    text = clean(text)
    first, _, rest = text.partition("\n")
    if rest or len(first) > _ARG_VALUE_CHARS:
        first = first[:_ARG_VALUE_CHARS - 1] + "…"
    return first


def _other_arguments(name: str, args: dict) -> str:
    """What the header shows after the key argument, dim: a `read`'s range, a
    `write`'s length, a `patch`'s edit count, then every remaining argument as
    `(key=value, …)`."""
    notes: list[str] = []
    span = args.get("range")
    if name == "read" and isinstance(span, dict):
        if "start_byte" in span or "end_byte" in span:
            notes.append(f"bytes {span.get('start_byte', 0)}-{span.get('end_byte', '')}")
        elif "start_line" in span or "end_line" in span:
            notes.append(f"lines {span.get('start_line', 1)}-{span.get('end_line', '')}")
    if name == "write" and isinstance(args.get("content"), str):
        notes.append(_count(len(args["content"].rstrip("\n").split("\n")), "line"))
    if name == "patch" and isinstance(args.get("edits"), list):
        notes.append(_count(len(args["edits"]), "edit"))
    left_out = {_key_name(name, args), _BODY_ARG.get(name), *_UNLISTED_ARGS.get(name, ())}
    rest = [f"{clean(str(key))}={_single_line(value)}" for key, value in args.items()
            if key not in left_out and value is not None and value != ""]
    if rest:
        notes.append("(" + ", ".join(rest) + ")")
    return " ".join(notes)


def _lexer(name: str, args: dict, body: str) -> str:
    if name in ("kernel", "toolbox"):
        return "python"
    if name in ("shell", "terminal_session"):
        first = body.lstrip().split("\n", 1)[0]
        return "python" if _PYTHON_FIRST_LINE.match(first) else "bash"
    if name == "plan":
        return "markdown"
    path = args.get("file_path") or args.get("path")
    if isinstance(path, str) and path:
        try:
            return Syntax.guess_lexer(path, body)
        except Exception:  # noqa: BLE001 - pygments lookup on an odd path
            return "text"
    return "text"


def _highlight(lines: list[str], lexer: str) -> list[Text]:
    """Each line as `lexer` source, one Text per input line."""
    if lexer in ("text", "default"):
        return [Text(line) for line in lines]
    code = "\n".join(lines)
    parts = list(Syntax(code, lexer, theme=fset.code_style(), background_color="default")
                 .highlight(code).split("\n", allow_blank=True))[:len(lines)]
    return parts if len(parts) == len(lines) else [Text(line) for line in lines]


def _fit(text: Text, room: int) -> list[Text]:
    """`text` as rows of at most `room` cells: a long line breaks at the
    column, as a terminal wraps it, and keeps all of itself."""
    text = text.copy()
    text.expand_tabs(8)
    cuts: list[int] = []
    used = 0
    for index, char in enumerate(text.plain):
        width = cell_len(char)
        if used + width > room and used:
            cuts.append(index)
            used = 0
        used += width
    return list(text.divide(cuts)) if cuts else [text]




class _Block:
    """The rows of one exchange, drawn at `width` behind a rail in the colour
    of `state`: running, finished or failed."""

    def __init__(self, width: int, colours: Theme, state: str = MARK) -> None:
        self.width = max(width, 24)
        self.inner = self.width - _RAIL
        self.theme = colours
        self.state = state
        self.rows: list[Text | None] = []

    def colour(self) -> str:
        return {MARK: self.theme.warn, OK: self.theme.ok, FAIL: self.theme.error}[self.state]

    def blank(self) -> None:
        """A row with no rail: the gap before an exchange."""
        self.rows.append(None)

    def header(self, name: str, args: dict | None, *, malformed: bool = False,
               about: str | None = None, rest: str | None = None,
               tail: str = "", tail_style: str = "") -> None:
        """`mark [name] key-argument  other-arguments[  tail]` on one row: the
        name on an accent chip. `about` and `rest` replace what the arguments
        would give."""
        th = self.theme
        args = args if isinstance(args, dict) else {}
        row = Text.assemble((f"{self.state} ", f"bold {self.colour()}"),
                            (f" {clean(name)} ", f"bold {th.panel} on {th.accent}"))
        if len(tail) > self.inner // 2:
            tail = tail[:self.inner // 2 - 1] + "…"
        reserve = len(tail) + 2 if tail else 0
        if malformed:
            row.append(" " + msgs.TOOL_ARGS_UNPARSED.body(), th.error)
        if about is None:
            about = key_argument(name, args, max(self.inner - row.cell_len - reserve - 1, _KEY_ARG_MIN))
        if about:
            row.append(" " + about, "bold")
        if rest is None:
            rest = _other_arguments(name, args)
        if rest:
            row.append("  " + rest, th.dim)
        row.truncate(max(self.inner - reserve, 8), overflow="ellipsis")
        if tail:
            row.append("  " + tail, tail_style or th.dim)
            row.truncate(self.inner, overflow="ellipsis")
        self.rows.append(row)

    def _panel_row(self, piece: Text) -> None:
        row = Text(" " * _PAD, style=f"{self.theme.panel_text} on {self.theme.panel}")
        row.append_text(piece)
        row.pad_right(self.inner - _INDENT - row.cell_len)
        self.rows.append(Text(" " * _INDENT) + row)

    def panel(self, lines: list[Text]) -> None:
        """`lines` on the panel background, padded to one width."""
        room = self.inner - _INDENT - 2 * _PAD
        for line in lines:
            for piece in _fit(line, room):
                self._panel_row(piece)

    def numbered(self, lines: list[tuple[str, Text]], *, sign: int = 0) -> None:
        """`(number, text)` rows as a file view on the panel: dim
        right-aligned numbers, the text beside them, a folded row continuing
        under its text. The first `sign` columns of a text, a diff's `+ ` or
        `- `, stand alone in their column: a folded row continues after them."""
        gutter = max((len(number) for number, _ in lines), default=1)
        room = self.inner - _INDENT - 2 * _PAD - gutter - 2 - sign
        for number, text in lines:
            head = text[:sign]
            for index, piece in enumerate(_fit(text[sign:], room)):
                label = number.rjust(gutter) if index == 0 else " " * gutter
                lead = head if index == 0 else Text(" " * sign)
                self._panel_row(Text.assemble((label, self.theme.dim), "  ", lead, piece))

    def note(self, text: str, style: str = "") -> None:
        row = Text(" " * (_INDENT + _PAD) + text, style or self.theme.dim)
        row.truncate(self.inner, overflow="ellipsis")
        self.rows.append(row)

    def render(self) -> str:
        if not self.rows:
            return ""
        console = _render_console(self.width)
        rail = Text.assemble((RAIL + " ", self.colour()))
        for row in self.rows:
            console.print(Text("") if row is None else rail + row, soft_wrap=True)
        return console.file.getvalue()  # type: ignore[attr-defined]


def _hidden(count: int) -> str:
    return msgs.TOOL_MORE_LINES.body(count=count)


def _names(settings: Any, key: str) -> frozenset[str]:
    value = _setting(settings, key)
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


# What a fold row counts, singular and plural; a tool not named counts calls.
_FOLD_NOUN: dict[str, tuple[str, str]] = {
    "read": ("file", "files"),
    "fs_search": ("search", "searches"),
    "ast_search": ("search", "searches"),
    "exa_search": ("search", "searches"),
    "serper_search": ("search", "searches"),
    "tavily_search": ("search", "searches"),
    "docs_search": ("search", "searches"),
    "fetch": ("page", "pages"),
    "browse": ("page", "pages"),
    "shell": ("look", "looks"),
}
_URL_HOST = re.compile(r"^[a-z][a-z0-9+.-]*://([^/?#]+)", re.I)


def fold_label(name: str, args: dict | None) -> str:
    """One call's name on a fold row: a path's last part, a URL's host, a
    shell command's own name, anything else the head of its key argument."""
    key = _key_name(name, args)
    if key is None:
        return ""
    text = clean(args[key]).strip().split("\n", 1)[0].strip()  # type: ignore[index]
    if key in _PATH_ARGS:
        return os.path.basename(text.rstrip("/")) or text
    host = _URL_HOST.match(text)
    if host is not None:
        return host.group(1)
    if name == "shell":
        words = text.split()
        return " ".join(words[:2]) if words[:1] == ["git"] else (words[0] if words else "")
    return text if len(text) <= 24 else text[:23] + "…"


def render_fold(name: str, calls: list[dict | None], *, running: int = 0, gap: bool = True,
                width: int | None = None, settings: Any = None) -> str:
    """Finished quiet calls of one tool as one row: `mark [name] N things
    label · label · +K`. `running` of them have not returned."""
    block = _Block(width or block_width(), theme(settings), MARK if running else OK)
    if gap:
        block.blank()
    one, many = _FOLD_NOUN.get(name, ("call", "calls"))
    about = f"{len(calls)} {one if len(calls) == 1 else many}"
    labels = [label for label in (fold_label(name, args) for args in calls) if label]
    room = block.inner - len(name) - len(about) - 10
    shown: list[str] = []
    for index, label in enumerate(labels):
        more = len(labels) - index - 1
        line = " · ".join([*shown, label]) + (f" · +{more}" if more else "")
        if len(line) > room and shown:
            shown.append(f"+{len(labels) - index}")
            break
        shown.append(label)
    block.header(name, None, about=about, rest=" · ".join(shown))
    return block.render()


def _read_view(output: ToolOutput, args: dict | None) -> tuple[list[tuple[str, Text]], int | None] | None:
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
    code = _highlight([text for _, text in kept], _lexer("read", args or {}, "\n".join(t for _, t in kept)))
    rows = [(number, text) for (number, _), text in zip(kept, code, strict=True)]
    return rows + [("", Text(note)) for note in notes], total


def _diff_view(output: ToolOutput, colours: Theme) -> tuple[list[tuple[str, Text]], str] | None:
    """A `patch` result as numbered diff rows and its summary; None when the
    result is not `patched … (summary, hash …)` followed by a unified diff."""
    if not output.lines:
        return None
    summary = _PATCHED.match(output.lines[0][0])
    if summary is None:
        return None
    rows: list[tuple[str, Text]] = []
    old = new = 0
    for line, _ in output.lines[1:]:
        hunk = _HUNK.match(line)
        if hunk is not None:
            if rows:
                rows.append(("", Text("⋯", colours.dim)))
            old, new = int(hunk.group(1)), int(hunk.group(2))
        elif line.startswith(("--- ", "+++ ")) and not old and not new:
            continue
        elif line.startswith("+"):
            rows.append((str(new), Text("+ " + line[1:], colours.added)))
            new += 1
        elif line.startswith("-"):
            rows.append((str(old), Text("- " + line[1:], colours.removed)))
            old += 1
        elif line.startswith(" "):
            rows.append((str(new), Text("  " + line[1:], colours.panel_text)))
            old, new = old + 1, new + 1
    return rows, summary.group(1)


# The argument a call's own text is in, shown at TOOLS_DISPLAY 1 in place of the
# result, which only acknowledges it.
_SAID_ARG: dict[str, str] = {"send": "text", "post_task": "body", "finish_task": "result"}
# Rows of a running command's output kept on screen.
LIVE_LINES = 6


def _last_line(output: ToolOutput) -> str:
    return next((line.strip() for line, _ in reversed(output.lines) if line.strip()), "")


def _program(block: _Block, name: str, args: dict, level: int, preview: int) -> None:
    """The call's program under its header at TOOLS_DISPLAY 2 and 3: a `write`'s
    file as a numbered view, any other body of more than one line as a panel."""
    body = args.get(_BODY_ARG.get(name, ""))
    if not isinstance(body, str) or not body.strip():
        return
    lines = clean(body).rstrip("\n").split("\n")
    if name != "write" and len(lines) == 1:
        return
    hidden = len(lines) - preview if level == 2 and len(lines) > preview else 0
    lines = lines[:len(lines) - hidden]
    code = _highlight(lines, _lexer(name, args, "\n".join(lines)))
    if name == "write":
        block.numbered([(str(n), text) for n, text in enumerate(code, 1)])
    else:
        block.panel(code)
    if hidden:
        block.note(_hidden(hidden))


def _result_rows(output: ToolOutput, colours: Theme) -> list[Text]:
    return [Text(line, colours.error if output.is_error else colours.warn if err else colours.panel_text)
            for line, err in output.lines]


def _capped(block: _Block, rows: list[Any], limit: int, draw: Callable[[list[Any]], None]) -> int:
    """Draw the first `limit` of `rows` and a note of how many are hidden."""
    draw(rows[:limit])
    if len(rows) > limit:
        block.note(_hidden(len(rows) - limit))
    return min(limit, len(rows))


def _finished_brief(block: _Block, name: str, args: dict, output: ToolOutput, kind: str,
                    preview: int, malformed: bool) -> None:
    """A finished exchange at TOOLS_DISPLAY 1."""
    colours = block.theme
    command = kind == "command" or name == "shell"
    if output.failed:
        if command:
            parts = [f"exit {output.exit_code}"] if output.exit_code else []
            last = _last_line(output)
            block.header(name, args, malformed=malformed,
                         tail=" · ".join([*parts, last] if last else parts), tail_style=colours.error)
            return
        if kind == "quiet" and len(output.lines) == 1:
            block.header(name, args, malformed=malformed, tail=output.lines[0][0].strip(),
                         tail_style=colours.error)
            return
        block.header(name, args, malformed=malformed)
        _capped(block, _result_rows(output, colours), preview, lambda rows: block.panel(rows))
        if output.tries is not None:
            block.note(msgs.TOOL_TRIES_LEFT.body(left=output.tries[0], allowed=output.tries[1]), colours.error)
        return
    if kind == "quiet":
        block.header(name, args, malformed=malformed)
        return
    if command:
        block.header(name, args, malformed=malformed, tail=_last_line(output))
        return
    diff = _diff_view(output, colours) if name == "patch" else None
    if diff is not None:
        rows, lead = diff
        block.header(name, args, malformed=malformed, tail=lead)
        _capped(block, rows, preview, lambda shown: block.numbered(shown, sign=2))
        return
    said = args.get(_SAID_ARG.get(name, ""))
    if isinstance(said, str) and said.strip():
        block.header(name, args, malformed=malformed, tail=_last_line(output))
        lines = [Text(line, colours.panel_text) for line in clean(said).strip("\n").split("\n")]
        _capped(block, lines, preview, lambda rows: block.panel(rows))
        return
    if kind == "diff" or len(output.lines) <= 1:
        block.header(name, args, malformed=malformed, tail=_last_line(output))
        return
    block.header(name, args, malformed=malformed)
    _capped(block, _result_rows(output, colours), preview, lambda rows: block.panel(rows))


def _finished_full(block: _Block, name: str, args: dict, output: ToolOutput, level: int,
                   preview: int, malformed: bool) -> None:
    """A finished exchange at TOOLS_DISPLAY 2 and 3: the program, the result as
    a file view, a diff or a panel, and its metrics."""
    colours = block.theme
    block.header(name, args, malformed=malformed)
    _program(block, name, args, level, preview)
    limit = preview if level == 2 else max(len(output.lines), 1)
    note_style = colours.error if output.failed else ""
    if not output.lines:
        block.note(metrics(output) if output.failed else msgs.TOOL_NO_OUTPUT.body(), note_style)
        return
    read = _read_view(output, args) if name == "read" else None
    diff = _diff_view(output, colours) if name == "patch" else None
    if read is not None:
        rows, of = read
        shown = _capped(block, rows, limit, lambda part: block.numbered(part))
        block.note(metrics(output, shown=shown, lines=len(rows), of=of), note_style)
    elif diff is not None:
        rows, lead = diff
        _capped(block, rows, limit, lambda part: block.numbered(part, sign=2))
        block.note(lead, note_style)
    else:
        shown = _capped(block, _result_rows(output, colours), limit, lambda part: block.panel(part))
        block.note(metrics(output, shown=shown), note_style)


def render_exchange(name: str, args: dict | None, result: Any = None, *, level: int,
                    preview: int = 12, width: int | None = None, malformed: bool = False,
                    settings: Any = None, live: list[str] | None = None, elapsed: str = "",
                    gap: bool = True) -> str:
    """One tool exchange behind its rail. `result` None is a call still
    running: `live` is the tail of its output so far, `elapsed` how long it
    has run.

    TOOLS_DISPLAY  1  what `call_kind` says of the call: a quiet call is one
                      row, a command its last line, a patch its diff, anything
                      else its result; a failure shows its error
                   2  the program and the first `preview` result lines
                   3  the program and every result line
    A long line wraps; none is cut.
    """
    if level <= 0:
        return ""
    colours = theme(settings)
    args = args if isinstance(args, dict) else {}
    if result is None:
        block = _Block(width or block_width(), colours, MARK)
        if gap:
            block.blank()
        block.header(name, args, malformed=malformed,
                     tail=msgs.TOOL_RUNNING.body(elapsed=elapsed) if elapsed else "")
        if level >= 2:
            _program(block, name, args, level, preview)
        if live:
            tail = [clean(line) for line in live][-LIVE_LINES:]
            block.panel([Text(line, colours.panel_text) for line in tail])
        return block.render()
    output = tool_output(name, result)
    block = _Block(width or block_width(), colours, FAIL if output.failed else OK)
    if gap:
        block.blank()
    if level == 1:
        _finished_brief(block, name, args, output, call_kind(name, args, settings), preview, malformed)
    else:
        _finished_full(block, name, args, output, level, preview, malformed)
    return block.render()


# --------------------------------------------------------------------------
# A resumed session's last exchanges
# --------------------------------------------------------------------------


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return "" if content is None else str(content)
    return "\n".join(
        part["text"] for part in content
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    )


def _call_args(call: dict) -> tuple[str, dict]:
    function = call.get("function") or {}
    raw = function.get("arguments")
    try:
        args = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        args = None
    return str(function.get("name") or "?"), args if isinstance(args, dict) else {}


def render_exchanges(messages: list[dict], count: int, *, width: int,
                     level: int, preview: int, markdown: bool = True,
                     settings: Any = None) -> str:
    """The last `count` exchanges of `messages` as a turn prints them: the
    user's line as `user_line` draws it, each tool exchange at TOOLS_DISPLAY
    `level`, the answer behind `assistant_mark()` as Markdown at `width` (plain
    text when `markdown` is off). An exchange starts at a user message that was
    not steered into a turn."""
    starts = [i for i, message in enumerate(messages)
              if message.get("role") == "user" and not message.get("steered")]
    if count <= 0 or not starts:
        return ""
    tail = messages[starts[-min(count, len(starts))]:]
    results = {message.get("tool_call_id"): message for message in tail
               if message.get("role") == "tool" and message.get("tool_call_id")}
    out: list[str] = []
    for message in tail:
        role = message.get("role")
        text = clean(_content_text(message.get("content"))).strip("\n")
        if role == "user":
            out.append(user_line(text))
        elif role == "assistant":
            if text.strip():
                out.append(assistant_mark())
                out.append(render_markdown(text, width) if markdown else text + "\n")
            for call in message.get("tool_calls") or []:
                name, args = _call_args(call)
                result = results.get(call.get("id"))
                out.append(render_exchange(
                    name, args, None if result is None else _content_text(result.get("content")),
                    level=level, preview=preview, width=width, settings=settings,
                ))
    return "".join(out)
