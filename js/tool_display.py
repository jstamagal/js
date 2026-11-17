"""Tool exchanges as the screen draws them.

The tool renderers build the exchange a turn prints at TOOLS_DISPLAY 0-3: a
header (mark, tool name, key argument, the other arguments dim), the program
or output in a panel, a file or a diff as a numbered view, and a dim footer
with the metrics. The colours are /fset's `COLOR_*` entries (`js.fset`).
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from typing import Any

from rich.syntax import Syntax
from rich.text import Text

from . import fset, messages as msgs, settings as _settings
from .control_sequences import SGR, clean
from .display import Theme, render_console, setting, theme
from .text_layout import TAB, block_width, cells, cut_points
from .toolkit.presentation import (
    PATH_ARGS,
    ScreenResult,
    call_kind,
    declared,
    tool_output,
)

# Transcript marker for a tool exchange: typeable ASCII, one per exchange, so a
# grep of a saved transcript finds every exchange the screen showed.
TOOL_MARKER = ">"


def tools_level(settings: Any) -> int:
    """TOOLS_DISPLAY from live settings: 0 nothing, 1 what matters, 2 preview, 3 whole."""
    level = setting(settings, "TOOLS_DISPLAY")
    return level if isinstance(level, int) and level in range(4) else _settings.default_value("TOOLS_DISPLAY")


def preview_lines(settings: Any) -> int:
    lines = setting(settings, "TOOLS_PREVIEW_LINES")
    return lines if isinstance(lines, int) and lines > 0 else _settings.default_value("TOOLS_PREVIEW_LINES")


# Screen marks of an exchange. `MARK` opens a call that has not finished;
# a finished one is `OK` or `FAIL`.
MARK = "…"
OK = "✓"
FAIL = "✗"
# The bar down the left edge of an exchange, in the colour of how it stands.
RAIL = "▌"
_RAIL = 2
# Columns a body sits in from the rail, and a panel's inner padding.
_INDENT = 1
_PAD = 1
# Columns a wrapped header row sits in from the rail: past the mark.
_HANG = 2


def for_transcript(rendered: str) -> str:
    """`rendered` as the transcript log records it: plain text without the
    rail, every exchange header opening with `TOOL_MARKER` whatever mark the
    screen drew."""
    rail = re.compile(rf"^{re.escape(fset.setting_value('EXCHANGE_RAIL'))} ?", re.M)
    marks = "|".join(re.escape(fset.setting_value(key)) for key in ("EXCHANGE_MARK_RUNNING", "EXCHANGE_MARK_OK", "EXCHANGE_MARK_FAILED"))
    header = re.compile(rf"^(?:{marks}) +", re.M)
    return header.sub(f"{TOOL_MARKER} ", rail.sub("", SGR.sub("", rendered)))


def _byte_len(text: str) -> int:
    return len(text.encode("utf-8", errors="replace"))


def _size(count: int) -> str:
    if count < 1024:
        return msgs.TOOL_SIZE_BYTES.words(count=count)
    if count < 1024 * 1024:
        return msgs.TOOL_SIZE_KILOBYTES.words(value=count / 1024)
    return msgs.TOOL_SIZE_MEGABYTES.words(value=count / (1024 * 1024))


def _count(number: int, noun: str) -> str:
    return msgs.COUNT_ONE.words(number=number, noun=noun) if number == 1 else msgs.COUNT_MANY.words(number=number, noun=noun)


def metrics(output: ScreenResult, *, shown: int | None = None, lines: int | None = None,
            of: int | None = None) -> str:
    """`[failed · L of N tries left · ][exit N · ]{lines} lines[ of {of}] · {size}`,
    plain text. `shown` is how many of the lines are on screen when some are
    hidden; `lines` overrides the result's own line count and `of` is the
    file's, for a `read` of one page."""
    parts: list[str] = []
    if output.tries is not None:
        parts.append(msgs.TOOL_FAILURE_TRIES_LEFT.words(left=output.tries[0], allowed=output.tries[1]))
    if output.exit_code:
        parts.append(msgs.TOOL_EXIT_CODE.words(exit_code=output.exit_code))
    total = len(output.lines) if lines is None else lines
    if of is not None and of > total:
        parts.append(msgs.TOOL_LINES_OF_TOTAL.words(total=total, value=_count(of, msgs.TOOL_LINE_NOUN.words())))
    else:
        parts.append(_count(total, msgs.TOOL_LINE_NOUN.words()))
    if shown is not None and shown < total:
        parts.append(msgs.TOOL_LINES_SHOWN.words(shown=shown))
    parts.append(_size(_byte_len(output.text)))
    return " · ".join(parts)


def short_path(path: str) -> str:
    """`path` under the working directory as a relative path, under the home
    directory as `~/…`, anything else unchanged. Run from the home directory
    itself, a path there is `~/…`."""
    home, cwd = os.path.expanduser("~"), os.getcwd()
    bases = ((home, "~/"),) if cwd.rstrip("/") == home.rstrip("/") else ((cwd, ""), (home, "~/"))
    for base, prefix in bases:
        if base not in ("", "/") and path.startswith(base.rstrip("/") + "/"):
            return prefix + path[len(base.rstrip("/")) + 1:]
    return path


def _key_name(name: str, args: dict | None) -> str | None:
    if not isinstance(args, dict):
        return None
    keys = declared(name).key
    return next((key for key in keys if isinstance(args.get(key), str) and args[key].strip()), None)


def key_argument(name: str, args: dict | None, room: int, whole: bool = False) -> str:
    """The call's key argument on one line of at most `room` cells: a path
    is shortened and keeps its tail, anything else its head; "" when the call
    has none. With ``whole`` every line of it, a newline shown as ⏎."""
    key = _key_name(name, args)
    if key is None:
        return ""
    lines = clean(args[key]).strip().split("\n")  # type: ignore[index]
    if whole:
        return fset.setting_value("ARGUMENT_NEWLINE_MARK").join(short_path(line.strip()) if key in PATH_ARGS else line.strip() for line in lines)
    text = lines[0].strip()
    more = fset.setting_value("DISPLAY_ELLIPSIS") if len(lines) > 1 else ""
    if key in PATH_ARGS:
        text = short_path(text)
    if len(text) + len(more) <= room:
        return text + more
    if key in PATH_ARGS:
        return fset.setting_value("DISPLAY_ELLIPSIS") + text[-(room - 1):]
    return text[:room - 1] + fset.setting_value("DISPLAY_ELLIPSIS")


def _single_line(value: Any, whole: bool = False) -> str:
    """An argument's value on one line: its head, cut at 40 columns, or with
    ``whole`` all of it, a newline shown as ⏎."""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(value)
    text = clean(text)
    if whole:
        return text.replace("\n", fset.setting_value("ARGUMENT_NEWLINE_MARK"))
    first, _, rest = text.partition("\n")
    maximum = int(fset.setting_value("ARGUMENT_CHARACTERS_MAX"))
    if rest or len(first) > maximum:
        first = first[:maximum - 1] + fset.setting_value("DISPLAY_ELLIPSIS")
    return first


def _other_arguments(name: str, args: dict, whole: bool = False) -> str:
    """What the header shows after the key argument, dim: the declared note
    (a range read, a body's length, an edit count), then every remaining
    argument as `(key=value, …)`."""
    shown = declared(name)
    body = shown.body
    notes: list[str] = []
    if shown.note == "range":
        span = args.get("range")
        if not isinstance(span, dict):
            span = {key: args[key] for key in ("start_line", "end_line", "start_byte", "end_byte") if key in args}
        span = {key: clean(value) if isinstance(value, str) else value for key, value in span.items()}
        if "range" in args:
            args = {**args, "range": span}
        if "start_line" in span or "end_line" in span:
            notes.append(msgs.TOOL_LINE_RANGE.words(value=span.get('start_line', 1), value2=span.get('end_line', '')))
        elif "start_byte" in span or "end_byte" in span:
            notes.append(msgs.TOOL_BYTE_RANGE.words(value=span.get('start_byte', 0), value2=span.get('end_byte', '')))
    if shown.note == "lines" and body is not None and isinstance(args.get(body), str):
        notes.append(_count(len(args[body].rstrip("\n").split("\n")), msgs.TOOL_LINE_NOUN.words()))
    if shown.note == "edits" and isinstance(args.get("edits"), list):
        notes.append(_count(len(args["edits"]), msgs.TOOL_EDIT_NOUN.words()))
    # The whole form leaves out only what it shows elsewhere: the key
    # argument, the body, a message's text.
    shown_elsewhere = () if whole else shown.unlisted
    left_out = {_key_name(name, args), body, shown.said, *shown_elsewhere}
    rest = [f"{clean(str(key))}={_single_line(value, whole)}" for key, value in args.items()
            if key not in left_out and value is not None and value != ""]
    if rest:
        notes.append("(" + ", ".join(rest) + ")")
    return clean(" ".join(notes))


def _lexer(name: str, args: dict, body: str) -> str:
    language = declared(name).language(body)
    return _path_lexer(args, body) if language is None else language


def _path_lexer(args: dict, body: str) -> str:
    """The lexer for `body`, guessed from the call's path."""
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
    text.expand_tabs(TAB)
    cuts = cut_points(text.plain, room, words=False)[:-1]
    return list(text.divide(cuts)) if cuts else [text]




class _Block:
    """The rows of one exchange, drawn at `width` behind a rail in the colour
    of `state`: running, finished or failed."""

    def __init__(self, width: int, colours: Theme, state: str = MARK) -> None:
        self.width = max(width, 10)
        self.inner = self.width - _RAIL
        self.theme = colours
        self.state = state
        self.rows: list[Text] = []
        self.whole = False  # TOOLS_DISPLAY 3: the header shows every argument whole

    def colour(self) -> str:
        return {MARK: self.theme.warn, OK: self.theme.ok, FAIL: self.theme.error}[self.state]

    def header(self, name: str, args: dict | None, *, malformed: bool = False,
               about: str | None = None, rest: str | None = None,
               tail: str | list[fset.Run] = "", tail_style: str = "") -> None:
        """`mark [name] key-argument  other-arguments`, the name in COLOR_TOOL
        on the panel gray; a row too long wraps under the name, nothing is
        cut. `tail`, the call's last word, sits on its own row below, behind
        TOOL_RESULT_LEAD. `about` and `rest` replace what the arguments would
        give."""
        th = self.theme
        args = args if isinstance(args, dict) else {}
        mark = fset.setting_value({MARK: "EXCHANGE_MARK_RUNNING", OK: "EXCHANGE_MARK_OK", FAIL: "EXCHANGE_MARK_FAILED"}[self.state])
        style = fset.setting_value("EXCHANGE_HEADER_STYLE")
        row = Text.assemble((f"{mark} ", f"{style} {self.colour()}"),
                            (f" {clean(name)} ", f"{style} {th.accent} on {th.panel}"))
        if malformed:
            row.append_text(Text(" ") + fset.to_rich(msgs.TOOL_ARGS_UNPARSED.body_runs(), th.error))
        if about is None:
            # A program shown whole in its own panel keeps only its first line here.
            whole = self.whole and declared(name).body != _key_name(name, args)
            about = key_argument(name, args, 10 ** 6, whole)
        if about:
            row.append(" " + about, fset.setting_value("EXCHANGE_ARGUMENT_STYLE"))
        if rest is None:
            rest = _other_arguments(name, args, self.whole)
        if rest:
            row.append("  " + rest, th.dim)
        self._hanging(row)
        if tail:
            self._hanging(fset.to_rich(msgs.TOOL_RESULT_LEAD.body_runs(), th.dim) + Text(" ")
                          + _piece(tail, tail_style or th.dim),
                          lead=_HANG)

    def _hanging(self, text: Text, lead: int = 0) -> None:
        """``text`` as rows: the first ``lead`` columns in, the rest under the
        text that follows the mark."""
        text = text.copy()
        text.expand_tabs(TAB)
        cuts = cut_points(text.plain, self.inner - lead, self.inner - _HANG - lead)
        pieces = text.divide(cuts[:-1]) if text.plain else []
        for index, piece in enumerate(pieces):
            if index:
                # The spaces at a break are the break: they start no row.
                piece = piece[len(piece.plain) - len(piece.plain.lstrip(" ")):]
                if not piece.plain:
                    continue
            if index < len(pieces) - 1:
                piece.rstrip()
            self.rows.append(Text(" " * (lead if index == 0 else _HANG + lead)) + piece)

    def _panel_row(self, piece: Text) -> None:
        row = Text(" " * _PAD, style=f"{self.theme.panel_text} on {self.theme.panel}")
        row.append_text(piece)
        row.pad_right(self.inner - _INDENT - cells(row.plain))
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

    def note(self, text: str | list[fset.Run], style: str = "") -> None:
        """A dim row of `text`, plain text or a message's colour runs,
        wrapped under itself when it is long."""
        self._hanging(_piece(text, style or self.theme.dim), lead=_INDENT + _PAD)

    def render(self) -> str:
        if not self.rows:
            return ""
        console = render_console(self.width)
        rail = Text.assemble((fset.setting_value("EXCHANGE_RAIL") + " ", self.colour()))
        for row in self.rows:
            console.print(rail + row, soft_wrap=True)
        return console.file.getvalue()  # type: ignore[attr-defined]


def _piece(text: str | list[fset.Run], style: str) -> Text:
    """Plain text, or a message's colour runs, as rich text on ``style``."""
    return fset.to_rich(text, style) if isinstance(text, list) else Text(text, style)


def _hidden(count: int) -> list[fset.Run]:
    return msgs.TOOL_MORE_LINES.body_runs(count=count)


_URL_HOST = re.compile(r"^[a-z][a-z0-9+.-]*://([^/?#]+)", re.I)


def fold_label(name: str, args: dict | None) -> str:
    """One call's name on a fold row: a path's last part, a URL's host, a
    shell command's own name, anything else the head of its key argument."""
    key = _key_name(name, args)
    if key is None:
        return ""
    text = clean(args[key]).strip().split("\n", 1)[0].strip()  # type: ignore[index]
    if key in PATH_ARGS:
        return os.path.basename(text.rstrip("/")) or text
    host = _URL_HOST.match(text)
    if host is not None:
        return host.group(1)
    if declared(name).fold_by_program:
        words = text.split()
        return " ".join(words[:2]) if words[:1] == ["git"] else (words[0] if words else "")
    return text if len(text) <= 24 else text[:23] + "…"


def render_fold(name: str, calls: list[dict | None], *, running: int = 0,
                width: int | None = None, settings: Any = None) -> str:
    """Finished quiet calls of one tool as one row: `mark [name] N things
    label · label · +K`. `running` of them have not returned."""
    block = _Block(width or block_width(), theme(), MARK if running else OK)
    one, many = msgs.TOOL_FOLD_NOUNS[declared(name).fold_noun]
    about = msgs.TOOL_FOLD_COUNT.words(value=len(calls), value2=one if len(calls) == 1 else many)
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


def _read_view(output: ScreenResult, args: dict | None) -> tuple[list[tuple[str, Text]], int | None] | None:
    if output.read is None:
        return None
    rows, total = output.read
    kept = [(number, text) for number, text in rows if number]
    notes = [(number, Text(text)) for number, text in rows if not number]
    code = _highlight([text for _, text in kept], _path_lexer(args or {}, "\n".join(t for _, t in kept)))
    return [(number, text) for (number, _), text in zip(kept, code, strict=True)] + notes, total


def _diff_view(output: ScreenResult, colours: Theme) -> tuple[list[tuple[str, Text]], str] | None:
    if output.diff is None:
        return None
    rows, summary = output.diff
    return [(number, Text(fset.setting_value("DIFF_HUNK_GAP"), colours.dim) if role == "gap"
             else Text(text, getattr(colours, role))) for number, text, role in rows], summary


def _last_line(output: ScreenResult) -> str:
    return next((line.strip() for line, _ in reversed(output.lines) if line.strip()), "")


def _program(block: _Block, name: str, args: dict, level: int, preview: int) -> None:
    """The call's program under its header at TOOLS_DISPLAY 2 and 3: a `write`'s
    file as a numbered view, any other body of more than one line as a panel."""
    shown = declared(name)
    body = args.get(shown.body or "")
    if not isinstance(body, str) or not body.strip():
        return
    lines = clean(body).rstrip("\n").split("\n")
    if not shown.body_is_file and len(lines) == 1 and _key_name(name, args) == shown.body:
        # A one-line program is the header's key argument already.
        return
    hidden = len(lines) - preview if level == 2 and len(lines) > preview else 0
    lines = lines[:len(lines) - hidden]
    code = _highlight(lines, _lexer(name, args, "\n".join(lines)))
    if shown.body_is_file:
        block.numbered([(str(n), text) for n, text in enumerate(code, 1)])
    else:
        block.panel(code)
    if hidden:
        block.note(_hidden(hidden))


def _result_rows(output: ScreenResult, colours: Theme) -> list[Text]:
    return [Text(line, colours.error if output.is_error else colours.warn if err else colours.panel_text)
            for line, err in output.lines]


def _capped(block: _Block, rows: list[Any], limit: int, draw: Callable[[list[Any]], None]) -> int:
    """Draw the first `limit` of `rows` and a note of how many are hidden."""
    draw(rows[:limit])
    if len(rows) > limit:
        block.note(_hidden(len(rows) - limit))
    return min(limit, len(rows))


def _finished_brief(block: _Block, name: str, args: dict, output: ScreenResult, kind: str,
                    preview: int, malformed: bool) -> None:
    """A finished exchange at TOOLS_DISPLAY 1."""
    colours = block.theme
    shown = declared(name)
    command = kind == "command" or shown.result == "command"
    if output.failed:
        if command:
            parts = [msgs.TOOL_EXIT_CODE.words(exit_code=output.exit_code)] if output.exit_code else []
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
            block.note(msgs.TOOL_TRIES_LEFT.body_runs(left=output.tries[0], allowed=output.tries[1]), colours.error)
        return
    if kind == "quiet":
        block.header(name, args, malformed=malformed)
        return
    if command:
        block.header(name, args, malformed=malformed, tail=_last_line(output))
        return
    diff = _diff_view(output, colours)
    if diff is not None:
        rows, lead = diff
        block.header(name, args, malformed=malformed, tail=lead)
        _capped(block, rows, preview, lambda shown: block.numbered(shown, sign=2))
        return
    said = args.get(shown.said or "")
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


def _finished_full(block: _Block, name: str, args: dict, output: ScreenResult, level: int,
                   preview: int, malformed: bool) -> None:
    """A finished exchange at TOOLS_DISPLAY 2 and 3: the program, the result as
    a file view, a diff or a panel, and its metrics."""
    colours = block.theme
    block.header(name, args, malformed=malformed)
    _program(block, name, args, level, preview)
    said = args.get(declared(name).said or "")
    if isinstance(said, str) and said.strip():
        # What the call itself said, which TOOLS_DISPLAY 1 shows in place of the result.
        lines = [Text(line, colours.panel_text) for line in clean(said).strip("\n").split("\n")]
        _capped(block, lines, preview if level == 2 else len(lines), lambda rows: block.panel(rows))
    limit = preview if level == 2 else max(len(output.lines), 1)
    note_style = colours.error if output.failed else ""
    if not output.lines:
        block.note(metrics(output) if output.failed else msgs.TOOL_NO_OUTPUT.body_runs(), note_style)
        return
    read = _read_view(output, args)
    diff = _diff_view(output, colours)
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
                    settings: Any = None, live: list[str] | None = None, elapsed: str = "") -> str:
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
    colours = theme()
    args = args if isinstance(args, dict) else {}
    if result is None:
        block = _Block(width or block_width(), colours, MARK)
        block.whole = level >= 3
        block.header(name, args, malformed=malformed,
                     tail=msgs.TOOL_RUNNING.body_runs(elapsed=elapsed) if elapsed else "")
        if level >= 2:
            _program(block, name, args, level, preview)
        if live:
            tail = [clean(line) for line in live][-int(setting(settings, "SHELL_LIVE_LINES_MAX")):]
            block.panel([Text(line, colours.panel_text) for line in tail])
        return block.render()
    output = tool_output(name, result)
    block = _Block(width or block_width(), colours, FAIL if output.failed else OK)
    block.whole = level >= 3
    if level == 1:
        _finished_brief(block, name, args, output, call_kind(name, args, settings), preview, malformed)
    else:
        _finished_full(block, name, args, output, level, preview, malformed)
    return block.render()
