"""Terminal presentation: model text and tool exchanges on their way to a screen.

Every byte of model or tool output passes `clean()` before a terminal sees it,
so a model that cats a binary cannot leave the terminal needing `reset`.

`Display` owns one stream of answer text. On a terminal it renders Markdown:
finished top-level blocks commit once and are never redrawn; only the last,
still-open block is live. Anywhere else it writes the cleaned text as it
arrives.

The tool renderers build the exchange a turn prints at `ui.tools` 0-3: a
header (mark, tool name, key argument, the other arguments dim), the program
or output in a panel, a file or a diff as a numbered view, and a dim footer
with the metrics. The colours are the `theme.*` settings.
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

from rich.console import Console
from rich.markdown import Markdown
from rich.syntax import Syntax
from rich.text import Text
from rich.theme import Theme as _RichTheme

from . import colors as C
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

# Chrome: harness-subroutine output, bold white on dark gray.
CHROME = "\033[1;97;48;2;48;48;48m"
CHROME_ERROR = "\033[1;91;48;2;48;48;48m"
CODE_THEME = "ansi_dark"

# The two voices of a conversation. The user's line sits behind the input
# prompt in bold; the answer opens with a dim mirrored glyph on its own line.
PROMPT = f"{C.BOLD}{C.YELLOW}{msgs.INPUT_PROMPT}{C.RESET}"
ASSISTANT_MARK = f"{C.GREY}❮{C.RESET}\n"


def user_line(text: str) -> str:
    """One user message as the conversation shows it."""
    return f"{PROMPT}{C.BOLD}{text}{C.RESET}\n"


def echo_user(text: str) -> None:
    """Print `user_line(text)` where the answer will follow."""
    sys.stdout.write(user_line(text))
    sys.stdout.flush()


def _setting(settings: Any, key: str) -> Any:
    """Knob ``key`` from live settings; its js/jsrc value when absent."""
    return _settings.knob(settings if isinstance(settings, dict) else None, key)


def tools_level(settings: Any) -> int:
    """`ui.tools` from live settings: 0 nothing, 1 metrics, 2 preview, 3 whole."""
    level = _setting(settings, "ui.tools")
    return level if isinstance(level, int) and level in range(4) else _settings.default_value("ui.tools")


def preview_lines(settings: Any) -> int:
    lines = _setting(settings, "ui.tools_preview_lines")
    return lines if isinstance(lines, int) and lines > 0 else _settings.default_value("ui.tools_preview_lines")


def markdown_enabled(settings: Any) -> bool:
    return _setting(settings, "ui.markdown") is not False


def terminal_width() -> int:
    return max(20, shutil.get_terminal_size((80, 24)).columns)


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


def render_markdown(source: str, width: int, settings: Any = None) -> str:
    """ANSI rendering of Markdown source at `width` columns: no blank lines
    before or after, ending in one newline. Inline code is a chip: its text in
    `theme.code` on the panel background, a space of padding each side."""
    colours = theme(settings)
    console = _render_console(width, {"markdown.code": f"{colours.code} on {colours.panel}"})
    document = Markdown(source, code_theme=CODE_THEME, hyperlinks=False)
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
MARK = "●"
OK = "✓"
FAIL = "✗"
_HEADER_MARK = re.compile(rf"^((?:\x1b\[[0-9;]*m)*)[{MARK}{OK}{FAIL}] ", re.M)
# Columns a body sits in from the left edge, and a panel's inner padding.
_INDENT = 2
_PAD = 1


@dataclass(frozen=True)
class Theme:
    """The colours of a tool exchange, each a `theme.<role>` setting."""

    accent: str      # tool name, running mark
    ok: str          # finished mark
    error: str       # failed mark, error text
    warn: str        # stderr
    dim: str         # arguments, line numbers, metrics
    panel: str       # output panel background
    panel_text: str  # output panel text
    added: str       # diff: added line
    removed: str     # diff: removed line
    code: str        # inline code in an answer


def theme(settings: Any = None) -> Theme:
    return Theme(**{role: str(_setting(settings, f"theme.{role}")) for role in Theme.__dataclass_fields__})


def for_transcript(rendered: str) -> str:
    """`rendered` as the transcript log records it: every exchange header
    opens with `TOOL_MARKER`, whatever mark the screen drew."""
    return _HEADER_MARK.sub(rf"\g<1>{TOOL_MARKER} ", rendered)


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
    parts = list(Syntax(code, lexer, theme=CODE_THEME, background_color="default")
                 .highlight(code).split("\n", allow_blank=True))[:len(lines)]
    return parts if len(parts) == len(lines) else [Text(line) for line in lines]


def _fit(text: Text, room: int, wrap: bool) -> list[Text]:
    """`text` as rows of at most `room` cells: folded when `wrap`, else one
    row cut with an ellipsis."""
    text = text.copy()
    text.expand_tabs(8)
    if not wrap:
        text.truncate(room, overflow="ellipsis")
        return [text]
    return list(text.wrap(_MEASURE, room, overflow="fold")) or [Text("")]


_MEASURE = Console(file=io.StringIO(), width=200)


class _Block:
    """The rows of one exchange, drawn at `width`."""

    def __init__(self, width: int, colours: Theme) -> None:
        self.width = max(width, 24)
        self.theme = colours
        self.rows: list[Text] = []

    def blank(self) -> None:
        self.rows.append(Text(""))

    def header(self, mark: str, name: str, args: dict | None, *, malformed: bool = False,
               tail: str = "", tail_style: str = "") -> None:
        """`mark name key-argument other-arguments[  tail]` on one row."""
        th = self.theme
        args = args if isinstance(args, dict) else {}
        colour = {MARK: th.accent, OK: th.ok, FAIL: th.error}[mark]
        row = Text.assemble((f"{mark} ", colour), (clean(name), f"bold {th.accent}"))
        room = self.width - row.cell_len - (len(tail) + 2 if tail else 0)
        if malformed:
            row.append(" " + msgs.TOOL_ARGS_UNPARSED.text(), th.error)
        about = key_argument(name, args, max(room - 1, _KEY_ARG_MIN))
        if about:
            row.append(" " + about)
        rest = _other_arguments(name, args)
        if rest:
            row.append(" " + rest, th.dim)
        limit = self.width - (len(tail) + 2 if tail else 0)
        row.truncate(max(limit, 8), overflow="ellipsis")
        if tail:
            row.append("  " + tail, tail_style or th.dim)
            row.truncate(self.width, overflow="ellipsis")
        self.rows.append(row)

    def panel(self, lines: list[Text], *, wrap: bool) -> None:
        """`lines` on the panel background, padded to one width."""
        inner = self.width - _INDENT - 2 * _PAD
        style = f"{self.theme.panel_text} on {self.theme.panel}"
        for line in lines:
            for piece in _fit(line, inner, wrap):
                row = Text(" " * _PAD, style=style)
                row.append_text(piece)
                row.pad_right(self.width - _INDENT - row.cell_len)
                row.stylize(f"on {self.theme.panel}")
                self.rows.append(Text(" " * _INDENT) + row)

    def numbered(self, lines: list[tuple[str, Text]], *, wrap: bool) -> None:
        """`(number, text)` rows as a file view: dim right-aligned numbers,
        the text beside them, a folded row continuing under its text."""
        gutter = max((len(number) for number, _ in lines), default=1)
        room = self.width - _INDENT - _PAD - gutter - 2
        for number, text in lines:
            for index, piece in enumerate(_fit(text, room, wrap)):
                label = number.rjust(gutter) if index == 0 else " " * gutter
                self.rows.append(Text.assemble(" " * (_INDENT + _PAD), (label, self.theme.dim), "  ", piece))

    def note(self, text: str, style: str = "") -> None:
        row = Text(" " * (_INDENT + _PAD) + text, style or self.theme.dim)
        row.truncate(self.width, overflow="ellipsis")
        self.rows.append(row)

    def footer(self, mark: str, text: str) -> None:
        colour = self.theme.ok if mark == OK else self.theme.error
        row = Text.assemble(" " * (_INDENT + _PAD), (f"{mark} ", colour),
                            (text, self.theme.error if mark == FAIL else self.theme.dim))
        row.truncate(self.width, overflow="ellipsis")
        self.rows.append(row)

    def render(self) -> str:
        if not self.rows:
            return ""
        console = _render_console(self.width)
        for row in self.rows:
            console.print(row, soft_wrap=True)
        return console.file.getvalue()  # type: ignore[attr-defined]


def _hidden(count: int) -> str:
    return msgs.TOOL_MORE_LINES.text(count=count)


def render_tool_call(name: str, args: dict | None, level: int, *, preview: int = 12,
                     width: int | None = None, malformed: bool = False,
                     settings: Any = None) -> str:
    """The call half of an exchange at `ui.tools` 2 and 3: a blank line, the
    header, then the tool's program in a panel when it runs past one line, or
    a `write`'s file as a numbered view (both capped at `preview` lines at
    level 2). Empty below level 2: level 1 shows only the result's line."""
    if level < 2:
        return ""
    block = _Block(width or terminal_width(), theme(settings))
    args = args if isinstance(args, dict) else {}
    block.blank()
    block.header(MARK, name, args, malformed=malformed)
    body = args.get(_BODY_ARG.get(name, ""))
    if isinstance(body, str) and body.strip():
        lines = clean(body).rstrip("\n").split("\n")
        if name == "write" or len(lines) > 1:
            hidden = len(lines) - preview if level == 2 and len(lines) > preview else 0
            lines = lines[:len(lines) - hidden]
            code = _highlight(lines, _lexer(name, args, "\n".join(lines)))
            block.blank()
            if name == "write":
                block.numbered([(str(n), text) for n, text in enumerate(code, 1)], wrap=level == 3)
                if hidden:
                    block.note(_hidden(hidden))
            else:
                block.panel(code + ([Text(_hidden(hidden), block.theme.dim)] if hidden else []),
                            wrap=level == 3)
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
            rows.append((str(new), Text("  " + line[1:])))
            old, new = old + 1, new + 1
    return rows, summary.group(1)


def render_tool_result(name: str, result: Any, level: int, *, preview: int = 12,
                       width: int | None = None, args: dict | None = None,
                       settings: Any = None) -> str:
    """The result half of an exchange.

    1  one row: the finished mark, the call as its header shows it, the metrics
    2  the first `preview` lines, long ones cut, then what is hidden and the metrics
    3  every line, long ones folded, then the metrics

    A `read` is a numbered file view, a `patch` a numbered diff, anything else
    a panel. A result of one line is the footer and has no panel.
    """
    if level <= 0:
        return ""
    colours = theme(settings)
    block = _Block(width or terminal_width(), colours)
    output = tool_output(name, result)
    mark = FAIL if output.failed else OK
    first = output.lines[0][0] if output.lines else ""
    one_line = len(output.lines) == 1 and name != "read" and not output.failed
    if level == 1:
        tail = first if output.failed and len(output.lines) == 1 else metrics(output)
        if len(tail) > block.width // 2:
            tail = tail[:block.width // 2 - 1] + "…"
        block.header(mark, name, args, tail=tail, tail_style=colours.error if output.failed else "")
        return block.render()
    if not output.lines:
        block.footer(mark, metrics(output) if output.failed else "no output")
        return block.render()
    if one_line:
        block.footer(mark, first)
        return block.render()
    wrap = level == 3
    limit = preview if level == 2 else len(output.lines)
    read = _read_view(output, args) if name == "read" else None
    diff = _diff_view(output, colours) if name == "patch" else None
    block.blank()
    of: int | None = None
    lead = ""
    if read is not None:
        rows, of = read
        block.numbered(rows[:limit], wrap=wrap)
        shown = min(limit, len(rows))
        hidden = len(rows) - shown
    elif diff is not None:
        rows, lead = diff
        block.numbered(rows[:limit], wrap=wrap)
        shown = min(limit, len(rows))
        hidden = len(rows) - shown
    else:
        shown = min(limit, len(output.lines))
        hidden = len(output.lines) - shown
        body = [Text(line, colours.error if output.is_error else colours.warn if err else "")
                for line, err in output.lines[:shown]]
        block.panel(body + ([Text(_hidden(hidden), colours.dim)] if hidden else []), wrap=wrap)
    if hidden and (read is not None or diff is not None):
        block.note(_hidden(hidden))
    if diff is not None:
        block.footer(mark, lead)
    elif read is not None:
        block.footer(mark, metrics(output, shown=shown, lines=len(rows), of=of))
    else:
        block.footer(mark, metrics(output, shown=shown))
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
    user's line as `user_line` draws it, each tool exchange at `ui.tools`
    `level`, the answer behind `ASSISTANT_MARK` as Markdown at `width` (plain
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
                out.append(ASSISTANT_MARK)
                out.append(render_markdown(text, width) if markdown else text + "\n")
            for call in message.get("tool_calls") or []:
                name, args = _call_args(call)
                out.append(render_tool_call(name, args, level, preview=preview, width=width,
                                            settings=settings))
                result = results.get(call.get("id"))
                if result is not None:
                    out.append(render_tool_result(
                        name, _content_text(result.get("content")), level,
                        preview=preview, width=width, args=args, settings=settings,
                    ))
    return "".join(out)
