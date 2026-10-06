"""Terminal presentation: model text and tool exchanges on their way to a screen.

Every byte of model or tool output passes `clean()` before a terminal sees it,
so a model that cats a binary cannot leave the terminal needing `reset`.

`Display` owns one stream of answer text. On a terminal it renders Markdown:
finished top-level blocks commit once and are never redrawn; only the last,
still-open block is live. Anywhere else it writes the cleaned text as it
arrives.

The tool renderers build the exchange a turn prints at `ui.tools` 0-3: a call
header that carries the transcript marker, the command as a highlighted block,
and the result with its metrics line.
"""

from __future__ import annotations

import io
import json
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

from . import colors as C
from . import messages as msgs
from . import settings as _settings

# String sequences (OSC, DCS, SOS, PM, APC) run to BEL or ST, 7- or 8-bit.
_STRING_SEQ = re.compile(r"(?:\x1b[\]PX^_]|[\x90\x98\x9d\x9e\x9f]).*?(?:\x07|\x1b\\|\x9c)", re.DOTALL)
_CSI = re.compile(r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]")
# Any other escape: ESC, intermediates, one final byte (ESC c, ESC ( 0, ESC 7).
_ESC = re.compile(r"\x1b[ -/]*[0-~]")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def clean(text: str) -> str:
    """Model and tool output as text, never as terminal control sequences.

    Removes OSC/DCS-style strings, CSI sequences and other escapes, then every
    C0/C1 control byte except newline and tab."""
    text = _STRING_SEQ.sub("", text)
    text = _CSI.sub("", text)
    text = _ESC.sub("", text)
    return _CONTROL.sub("", text)


# Transcript marker for a tool exchange: typeable ASCII, one per exchange, so a
# grep of a saved transcript finds every exchange the screen showed.
TOOL_MARKER = ">"

# Chrome: harness-subroutine output, bold white on dark gray.
CHROME = "\033[1;97;48;2;48;48;48m"
CHROME_BODY = "\033[97;48;2;48;48;48m"
CHROME_ERROR = "\033[1;91;48;2;48;48;48m"
CHROME_STDERR = "\033[93;48;2;48;48;48m"
STDERR = "\033[93m"
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


def _render_console(width: int) -> Console:
    return Console(
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


def render_markdown(source: str, width: int) -> str:
    """ANSI rendering of Markdown source at `width` columns: no blank lines
    before or after, ending in one newline."""
    console = _render_console(width)
    console.print(Markdown(source, code_theme=CODE_THEME, hyperlinks=False))
    return _rendered_text(console).strip("\n") + "\n"


def render_code(code: str, lexer: str, width: int) -> str:
    console = _render_console(width)
    console.print(Syntax(code, lexer, theme=CODE_THEME, word_wrap=True, background_color="default"))
    text = _rendered_text(console)
    return text if text.endswith("\n") else text + "\n"


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
        now = time.monotonic()
        if now - self._refreshed >= self.refresh_s:
            self._refresh()
            self._refreshed = now

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
            lines = value.splitlines(keepends=True)
            cut = starts[-1]
            self._commit("".join(lines[:cut]))
            value = "".join(lines[cut:])
            self._source = value
        if value.strip():
            separator = "\n" if self._committed_any else ""
            self.live.update(separator + render_markdown(value, self.live.width()))

    def finish(self) -> None:
        """Commit whatever is open and end the stream on a fresh line."""
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
    "patch": "new_string",
    "plan": "content",
}
_HEADER_VALUE_CHARS = 80
_SHELL_HEADER = re.compile(r"shell=[^\n]*\nexit=(-?\d+)\n")
_SHELL_SECTION = re.compile(r"^--- (stdout|stderr) ---$")
_PYTHON_FIRST_LINE = re.compile(r"^\s*(?:\S*/)?python[0-9.]*\b")
# A `read` result line: `N|` before the file's own text.
_READ_GUTTER = re.compile(r"^\d+\|")


@dataclass
class ToolOutput:
    """A tool result as the screen shows it: cleaned lines with their stream."""

    lines: list[tuple[str, bool]]  # (text, is_stderr)
    exit_code: int | None = None
    is_error: bool = False

    @property
    def text(self) -> str:
        return "\n".join(line for line, _ in self.lines)


def result_text(result: Any) -> tuple[str, bool]:
    """Flatten a tool result to (text, is_error)."""
    dehydrated = getattr(result, "dehydrated", None)
    if callable(dehydrated):
        return dehydrated(), bool(getattr(result, "is_error", False))
    text = result if isinstance(result, str) else str(result)
    return text, text.lstrip().startswith("ERROR")


def tool_output(name: str, result: Any) -> ToolOutput:
    """Parse a result for display. A finished shell result loses its
    `shell=… exit=… --- stdout ---` envelope; exit is kept apart."""
    text, is_error = result_text(result)
    text = clean(text)
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
    return ToolOutput([(line, False) for line in body.split("\n")] if body else [], is_error=is_error)


def _byte_len(text: str) -> int:
    return len(text.encode("utf-8", errors="replace"))


def metrics_line(name: str, output: ToolOutput, *, shown: ToolOutput | None = None) -> str:
    """`{tool}: [exit N ]{shown/}{bytes}B {shown/}{lines}L`, plain text."""
    total_bytes = _byte_len(output.text)
    total_lines = len(output.lines)
    exit_part = f"exit {output.exit_code} " if output.exit_code else ""
    if shown is None:
        return f"{name}: {exit_part}{total_bytes}B {total_lines}L"
    return (
        f"{name}: {exit_part}{_byte_len(shown.text)}/{total_bytes}B "
        f"{len(shown.lines)}/{total_lines}L"
    )


# The argument that tells one call of a tool from another on its one-line
# trace: the first of these that the call carries. A tool not named here is
# told apart by its path, when it has one.
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


def key_argument(name: str, args: dict | None, room: int) -> str:
    """The call's key argument on one line of at most `room` cells: a path
    keeps its tail, anything else its head; "" when the call has none."""
    if not isinstance(args, dict):
        return ""
    keys = _KEY_ARGS.get(name, _PATH_ARGS)
    value = next((args[key] for key in keys if isinstance(args.get(key), str) and args[key].strip()), None)
    if value is None:
        return ""
    lines = clean(value).strip().split("\n")
    text = lines[0].strip()
    more = "…" if len(lines) > 1 else ""
    if len(text) + len(more) <= room:
        return text + more
    if keys is _PATH_ARGS:
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
    if rest or len(first) > _HEADER_VALUE_CHARS:
        first = first[:_HEADER_VALUE_CHARS - 3] + "..."
    return first


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


def render_tool_call(name: str, args: dict | None, level: int, *, preview: int = 12,
                     width: int | None = None, malformed: bool = False) -> str:
    """The call half of an exchange at `ui.tools` 2 and 3: the marker header,
    then the tool's program or body as a highlighted block (capped at `preview`
    lines at level 2). Empty below level 2: level 1 shows only the metrics."""
    if level < 2:
        return ""
    width = width or terminal_width()
    args = args if isinstance(args, dict) else {}
    body_key = _BODY_ARG.get(name)
    parts = [f"{TOOL_MARKER} {clean(name)}"]
    if malformed:
        parts.append(msgs.TOOL_ARGS_UNPARSED.text())
    for key, value in args.items():
        if key == body_key or value is None or value == "":
            continue
        parts.append(f"{clean(str(key))}={_single_line(value)}")
    out = [f"{CHROME}{' '.join(parts)}{C.RESET}\n"]
    body = args.get(body_key) if body_key else None
    if isinstance(body, str) and body.strip():
        code = clean(body).rstrip("\n")
        lines = code.split("\n")
        hidden = 0
        if level == 2 and len(lines) > preview:
            hidden = len(lines) - preview
            code = "\n".join(lines[:preview])
        out.append(render_code(code, _lexer(name, args, code), width))
        if hidden:
            out.append(f"{CHROME}{msgs.TOOL_MORE_LINES.text(count=hidden)}{C.RESET}\n")
    return "".join(out)


def _highlight_lines(lines: list[str], lexer: str, width: int) -> list[str]:
    """Each line highlighted as `lexer` source, one output line per input line."""
    code = "\n".join(lines)
    text = Syntax(code, lexer, theme=CODE_THEME, background_color="default").highlight(code)
    parts = text.split("\n", allow_blank=True)[:len(lines)]
    console = _render_console(width)
    out = []
    for part in parts:
        with console.capture() as captured:
            console.print(part, end="", soft_wrap=True)
        out.append(captured.get())
    return out if len(out) == len(lines) else lines


def _read_body_lines(lines: list[tuple[str, bool]], args: dict | None,
                     width: int) -> list[str] | None:
    """A `read` result's lines with the file text highlighted by the file's
    type. `N|` gutters, and lines without one when others have one (the
    paging note), stay plain. None when the file type has no lexer."""
    if not isinstance(args, dict) or not lines:
        return None
    matches = [_READ_GUTTER.match(line) for line, _ in lines]
    gutters = any(match is not None for match in matches)
    code_rows = [i for i, match in enumerate(matches) if match is not None or not gutters]
    content = [lines[i][0][matches[i].end():] if matches[i] else lines[i][0] for i in code_rows]
    lexer = _lexer("read", args, "\n".join(content))
    if lexer in ("text", "default"):
        return None
    highlighted = _highlight_lines(content, lexer, width)
    out = [line for line, _ in lines]
    for i, body in zip(code_rows, highlighted, strict=True):
        out[i] = (matches[i].group(0) if matches[i] else "") + body
    return out


def render_tool_result(name: str, result: Any, level: int, *, preview: int = 12,
                       width: int | None = None, args: dict | None = None) -> str:
    """The result half of an exchange.

    1  one metrics line, carrying the marker (level 1 prints no call header)
    2  the first `preview` lines in chrome, `...`, and shown/total metrics
    3  every line, then the metrics line; a `read` of source code is
       highlighted by the file's type (`args` carries its path)
    """
    if level <= 0:
        return ""
    width = width or terminal_width()
    output = tool_output(name, result)
    head = CHROME_ERROR if output.is_error or output.exit_code else CHROME
    if level == 1:
        room = width - len(TOOL_MARKER) - len(metrics_line(name, output)) - 3
        about = key_argument(name, args, max(room, _KEY_ARG_MIN))
        label = f"{name} {about}" if about else name
        return f"{head}{TOOL_MARKER} {metrics_line(label, output)}{C.RESET}\n"
    out: list[str] = []
    if level == 2:
        clip = max(20, width - 1)
        kept = [(line[:clip], err) for line, err in output.lines[:preview]]
        shown = ToolOutput(kept, exit_code=output.exit_code, is_error=output.is_error)
        for line, err in kept:
            out.append(f"{CHROME_STDERR if err else CHROME_BODY}{line}{C.RESET}\n")
        if shown.text != output.text:
            out.append("...\n")
            out.append(f"{head}{metrics_line(name, output, shown=shown)}{C.RESET}\n")
        else:
            out.append(f"{head}{metrics_line(name, output)}{C.RESET}\n")
        return "".join(out)
    highlighted = _read_body_lines(output.lines, args, width) if name == "read" else None
    if highlighted is not None:
        out.extend(f"{line}{C.RESET}\n" for line in highlighted)
    else:
        for line, err in output.lines:
            out.append(f"{STDERR}{line}{C.RESET}\n" if err else f"{line}\n")
    out.append(f"{head}{metrics_line(name, output)}{C.RESET}\n")
    return "".join(out)


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
                     level: int, preview: int, markdown: bool = True) -> str:
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
    results = {message.get("tool_call_id"): message for message in tail if message.get("role") == "tool"}
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
                out.append(render_tool_call(name, args, level, preview=preview, width=width))
                result = results.get(call.get("id"))
                if result is not None:
                    out.append(render_tool_result(
                        name, _content_text(result.get("content")), level,
                        preview=preview, width=width, args=args,
                    ))
    return "".join(out)
