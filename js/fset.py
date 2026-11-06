"""/fset: how everything js prints looks.

`/set` is behavior; `/fset` is everything that looks like something. Its
entries are:

  formats    one per `js.messages` entry, named like it (`MODEL_SET`). The
             text the screen shows for that message.
  palette    `PALETTE_00` .. `PALETTE_98`: the mIRC 99-colour palette, each
             slot a `#rrggbb` js sends as 24-bit colour.
  colours    `COLOR_<ROLE>`: what each part of the screen is drawn in.
  THEME      the built-in theme under the entries the operator set.
  HOST_COLOR this box's colour; the themes point the prompt, the speaker
             marks and the status bar's accent at it.
  CODE_STYLE the Pygments style code is highlighted in.

Display levels (`TOOLS_DISPLAY` and the rest) are /fset entries too; they
live in the settings registry with `command="fset"` so a turn and a subagent
read them like any setting. This module holds the rest.

An entry's value is the operator's (an `fset` line in a jsrc, or typed), else
the theme's (`js/themes/<THEME>`), else the default (`js/fset`).

A format is literal text, colour codes, the banner glyph and holes. An event
hands its format one line of text, split on single spaces into words
numbered from 0; every field but the last is one word, and only the last
may contain spaces.

  $0 .. $9  that word.
  $N-       word N through the end of the line.
  $*        the whole line.
  %Cnn      what follows is drawn in palette slot nn, 00 to 98.
  %C        what follows is drawn in the terminal's own colour.
  %G        the banner glyph, the BANNER setting.
  %%  $$    a literal % and $.

A format may use any word, in any order, or leave any out. Trailing spaces
are trimmed. Setting a format checks it (`format_error`); rendering never
raises.

A colour value is `#rrggbb`, a two-digit palette slot, or `$NAME` of another
colour entry; `a|b` takes the first that resolves to a colour.
"""

from __future__ import annotations

import contextlib
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

PALETTE_SIZE = 99
PACKAGE_FSET = Path(__file__).with_name("fset")
THEMES_DIR = Path(__file__).with_name("themes")

RESET = "\x1b[0m"
FG_DEFAULT = "\x1b[39m"  # the foreground back to the default, the background kept
BOLD = "\x1b[1m"

# Every colour role; `js/help/fset.txt` says what each paints.
ROLES: tuple[str, ...] = (
    "COLOR_BACKGROUND", "COLOR_TEXT", "COLOR_DIM", "COLOR_USER", "COLOR_USER_MARK", "COLOR_AGENT",
    "COLOR_AGENT_MARK", "COLOR_WARN", "COLOR_ERROR", "COLOR_OK", "COLOR_TOOL",
    "COLOR_TOOL_OUTPUT", "COLOR_PANEL", "COLOR_ACCENT", "COLOR_ADDED", "COLOR_REMOVED", "COLOR_CODE",
    "COLOR_REASONING", "COLOR_SEPARATOR", "COLOR_STATUS_BACKGROUND", "COLOR_STATUS_TEXT",
    "COLOR_STATUS_ACCENT",
)
# Roles a theme may leave empty.
OPTIONAL_ROLES = frozenset({"COLOR_BACKGROUND"})

THEME = "THEME"
HOST_COLOR = "HOST_COLOR"
CODE_STYLE = "CODE_STYLE"
# The entries that are neither a colour, a palette slot nor a format.
FIXED: tuple[str, ...] = (THEME, HOST_COLOR, CODE_STYLE, "MARK_USER", "MARK_AGENT", "MARK_GAP")

_PALETTE_NAME = re.compile(r"PALETTE_(\d\d)")
_HEX = re.compile(r"#[0-9a-fA-F]{6}")
_DIGITS = re.compile(r"[0-9]+")
_SLOT = re.compile(r"\d\d")
_TOKEN = re.compile(r"%%|%G|%C(\d\d)?|\$\$|\$\*|\$(\d)(-)?")
_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")
# Variation selectors and skin-tone modifiers: terminals disagree on their width.
_MARK_MODIFIER = re.compile("[\uFE0E\uFE0F\U0001F3FB-\U0001F3FF]")


def palette_name(slot: int) -> str:
    return f"PALETTE_{slot:02d}"


# ---------------------------------------------------------------------------
# The three layers
# ---------------------------------------------------------------------------

_defaults: dict[str, str] | None = None
_themes: dict[str, dict[str, str]] = {}
_overrides: dict[str, str] = {}
_settings: dict | None = None


def parse_line(line: str) -> tuple[str, str] | None:
    """``(NAME, value)`` of an `fset NAME value` line, else None, read the way
    a jsrc line is: words split at any whitespace (`setcmd.first_word`), a
    JSON string literal the string it spells (`settings.from_line_literal`)."""
    from . import setcmd  # lazy: setcmd imports this module
    from . import settings as _s

    parsed = setcmd.split_command(line)
    if parsed is None or parsed[0] != "fset":
        return None
    name, value = setcmd.first_word(parsed[1])
    return (name, _s.from_line_literal(value)) if name else None


def read_file(path: Path) -> dict[str, str]:
    """The entries an fset file sets: `fset NAME value` lines; `#` comments and
    blank lines are skipped."""
    entries: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parsed = parse_line(line)
        if parsed is not None:
            entries[parsed[0]] = parsed[1]
    return entries


def defaults() -> dict[str, str]:
    """The entries `js/fset` sets: the palette, THEME and the formats that are
    not their message's own text."""
    global _defaults
    if _defaults is None:
        _defaults = read_file(PACKAGE_FSET)
    return _defaults


def themes() -> list[str]:
    """The built-in themes, by name."""
    return sorted(path.name for path in THEMES_DIR.iterdir() if path.is_file() and not path.name.startswith("."))


def theme_entries(name: str) -> dict[str, str]:
    if name not in _themes:
        path = THEMES_DIR / name
        _themes[name] = read_file(path) if path.is_file() else {}
    return _themes[name]


def theme_name() -> str:
    name = _overrides.get(THEME) or defaults().get(THEME, "")
    return name if name in themes() else defaults().get(THEME, "")


def lookup(name: str, *, transform_default=None) -> str | None:
    """Entry ``name``'s value: the operator's, the theme's, else the default;
    None when none of them sets it."""
    if name in _overrides:
        return _overrides[name]
    if name == THEME:
        return theme_name()
    theme = theme_entries(theme_name())
    if name in theme:
        return theme[name]
    value = defaults().get(name)
    return transform_default(value) if value is not None and transform_default is not None else value


def entry_value(name: str) -> str | None:
    """What `$NAME` of an /fset entry stands for: its value (`lookup`), or for
    a format nobody set, the format its message has by default."""
    found = lookup(name)
    if found is None:
        from . import messages as _msgs  # lazy: messages imports this

        entry = _msgs.ENTRIES.get(name)
        found = entry.default_format() if entry is not None else None
    return found


def overrides() -> dict[str, str]:
    """The entries the operator set, as `/save` writes them."""
    return dict(_overrides)


def reset() -> None:
    """Forget every entry the operator set."""
    _overrides.clear()


@contextlib.contextmanager
def held() -> Iterator[None]:
    """A block that starts with no entry set and, when it ends, gives the
    operator's entries back as they were."""
    saved = dict(_overrides)
    _overrides.clear()
    try:
        yield
    finally:
        _overrides.clear()
        _overrides.update(saved)


def setting_value(name: str):
    """A registered setting from the bound live store, or its shipped default."""
    from . import settings as _s

    return _s.knob(_settings, name)


def bind_settings(settings: dict | None) -> None:
    """The live settings `$NAME` reads."""
    global _settings
    _settings = settings
    from . import settings as _s

    _s.bind_live_settings(settings)


_NAME = re.compile(r"\$(\$|[A-Z][A-Z0-9_]*)")


def expand_names(text: str, settings: dict | None = None) -> str:
    """``text`` with each `$NAME` of a setting or /fset entry replaced by its
    value, from ``settings`` (else the bound live settings); `$$` is a `$`.
    One pass: a value put in is never read again. Any other `$` stays."""
    from . import settings as _s  # lazy: settings imports messages, which imports this

    def value(match: re.Match) -> str:
        name = match.group(1)
        if name == "$":
            return "$"
        spec = _s.spec_for(name)
        if spec is not None:
            found = _s.knob(settings if settings is not None else _settings, spec.key)
            return match.group(0) if found is None else _s.config_value_text(found)
        if is_name(name):
            found = entry_value(name)
            return match.group(0) if found is None else found
        return match.group(0)

    return _NAME.sub(value, text)


def read_when_drawn(name: str) -> bool:
    """Whether entry ``name`` keeps its `$NAME`s until it is drawn: a format
    or a colour. Any other entry's line expands when it loads, as `set` does."""
    from . import messages as _msgs

    return name in _msgs.ENTRIES or is_colour_name(name)


def banner_text() -> str:
    """The banner slot, the BANNER setting: what starts every line where js
    reports its state."""
    from . import messages as _msgs  # lazy: messages imports this

    return _msgs.current_banner()


# ---------------------------------------------------------------------------
# Names and values
# ---------------------------------------------------------------------------

def format_names() -> list[str]:
    from . import messages as _msgs  # lazy: messages imports this

    return sorted(_msgs.ENTRIES)


def is_colour_name(name: str) -> bool:
    return name in ROLES or name == HOST_COLOR or _PALETTE_NAME.fullmatch(name) is not None and \
        int(name[-2:]) < PALETTE_SIZE


def is_name(name: str) -> bool:
    """``name`` is an fset entry this module holds."""
    from . import messages as _msgs

    return name in FIXED or is_colour_name(name) or name in _msgs.ENTRIES


def names() -> list[str]:
    """Every entry this module holds, in the order `/fset` lists them."""
    return [*FIXED, *ROLES, *(palette_name(n) for n in range(PALETTE_SIZE)),
            *format_names()]


def check(name: str, value: str) -> str | None:
    """Why ``value`` cannot be entry ``name``'s, or None."""
    from . import messages as _msgs

    if not is_name(name):
        return _msgs.FSET_UNKNOWN.said(name=name)
    if name == THEME and value not in themes():
        return _msgs.EXPECTED_ONE_OF.said(choices="|".join(themes()))
    if name == CODE_STYLE:
        from pygments.styles import get_all_styles

        if value not in set(get_all_styles()):
            return _msgs.FSET_NO_CODE_STYLE.said(value=value)
    if name == "MARK_GAP" and not _DIGITS.fullmatch(value):
        return _msgs.EXPECTED_NONNEGATIVE_INTEGER.said()
    if name in ("MARK_USER", "MARK_AGENT") and _MARK_MODIFIER.search(value):
        return _msgs.FSET_MARK_MODIFIER.said()
    if _PALETTE_NAME.fullmatch(name) and not _HEX.fullmatch(value):
        return _msgs.EXPECTED_COLOUR.said(value=value)
    if name in _msgs.ENTRIES:
        return format_error(value, len(_msgs.ENTRIES[name].fields()))
    if name in ROLES or name == HOST_COLOR:
        if value and not all(_HEX.fullmatch(part) or _SLOT.fullmatch(part) or part.startswith("$")
                             for part in value.split("|")):
            return _msgs.EXPECTED_COLOUR_VALUE.said(value=value)
    return None


def set_entry(name: str, value: str) -> str | None:
    """Set entry ``name`` to ``value``; why not, or None."""
    error = check(name, value)
    if error is None:
        _overrides[name] = value
    return error


def unset_entry(name: str) -> bool:
    """Drop the operator's value of ``name``; whether there was one."""
    return _overrides.pop(name, None) is not None


# ---------------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------------

def hex_for(name: str, _depth: int = 0) -> str | None:
    """Colour entry ``name`` as `#rrggbb`, or None when it resolves to none."""
    if _depth > 8:
        return None
    value = lookup(name) or ""
    for part in value.split("|"):
        part = part.strip()
        if _HEX.fullmatch(part):
            return part.lower()
        if _SLOT.fullmatch(part):
            found = hex_for(palette_name(int(part)), _depth + 1) if int(part) < PALETTE_SIZE else None
        elif part.startswith("$") and is_colour_name(part[1:]):
            found = hex_for(part[1:], _depth + 1)
        else:
            found = None
        if found is not None:
            return found
    return None


def _rgb(colour: str) -> str:
    return f"{int(colour[1:3], 16)};{int(colour[3:5], 16)};{int(colour[5:7], 16)}"


def no_color() -> bool:
    """NO_COLOR is set (no-color.org)."""
    return bool(os.environ.get("NO_COLOR"))


def fg(name: str) -> str:
    """The escape that paints text in colour entry ``name``; "" for none, or
    under NO_COLOR."""
    if not colour_on():
        return ""
    colour = hex_for(name)
    return f"\x1b[38;2;{_rgb(colour)}m" if colour else ""


def bg(name: str) -> str:
    """The escape that paints the background in colour entry ``name``; "" for
    none, or under NO_COLOR."""
    if not colour_on():
        return ""
    colour = hex_for(name)
    return f"\x1b[48;2;{_rgb(colour)}m" if colour else ""


def tint(name: str, text: str) -> str:
    """``text``, which may carry colour of its own, in colour entry ``name``
    wherever its own colour is reset."""
    code = fg(name)
    if not code or not text:
        return text
    return code + text.replace(RESET, RESET + code) + RESET


def strong(name: str, text: str) -> str:
    """``text`` in bold and colour entry ``name``, closed by a reset; no
    colour under NO_COLOR or when ``name`` resolves to none."""
    return f"{BOLD}{fg(name)}{text}{RESET}" if text else text


def paint(name: str, text: str) -> str:
    """``text`` in colour entry ``name``."""
    code = fg(name)
    return f"{code}{text}{RESET}" if code and text else text


def code_style() -> str:
    """The Pygments style code is highlighted in."""
    from pygments.styles import get_all_styles

    style = lookup(CODE_STYLE) or ""
    return style if style in set(get_all_styles()) else "default"


def plain(text: str) -> str:
    """``text`` without colour escapes."""
    return _ESCAPE.sub("", text)


# ---------------------------------------------------------------------------
# Formats
# ---------------------------------------------------------------------------

def escape(text: str) -> str:
    """``text`` as a format that renders it as written."""
    return text.replace("%", "%%").replace("$", "$$")


# A run of text and the colour it is drawn in: "#rrggbb", or "" for the
# terminal's own. What a format renders to, before any output target sees it.
Run = tuple[str, str]
# A word an event passes: text, or runs (a message inside another, which
# keeps its own colours).
Word = str | list[Run]


def _split_word(value: Word) -> list[Word]:
    """``value`` cut at each single space into words."""
    if isinstance(value, str):
        return value.split(" ")
    words: list[Word] = [[]]
    for colour, text in value:
        for index, piece in enumerate(text.split(" ")):
            if index:
                words.append([])
            if piece:
                words[-1].append((colour, piece))
    return words


def words(fields: list[Word]) -> list[Word]:
    """The words of the line an event hands its format: every field but the
    last is one word, and the last is cut at each single space."""
    if not fields:
        return []
    return [*fields[:-1], *_split_word(fields[-1])]


def format_error(fmt: str, fields: int) -> str | None:
    """Why ``fmt`` is not a format for an event that passes ``fields``
    fields, or None. Each `%` and `$` starts a code or a hole; a hole names
    a field the event passes."""
    from . import messages as _msgs

    pos = 0
    for match in _TOKEN.finditer(fmt):
        stray = re.search(r"[%$]", fmt[pos:match.start()])
        if stray:
            return _msgs.FSET_BAD_CODE.said(code=fmt[pos + stray.start():pos + stray.start() + 2])
        pos = match.end()
        token = match.group(0)
        slot = match.group(1)
        if slot is not None and int(slot) >= PALETTE_SIZE:
            return _msgs.FSET_BAD_SLOT.said(code=token)
        word = match.group(2)
        if word is not None and int(word) >= fields:
            return (_msgs.FSET_NO_HOLES.said(hole=token) if not fields
                    else _msgs.FSET_BAD_HOLE.said(hole=token, last=fields - 1))
        if token == "$*" and not fields:
            return _msgs.FSET_NO_HOLES.said(hole=token)
    stray = re.search(r"[%$]", fmt[pos:])
    if stray:
        return _msgs.FSET_BAD_CODE.said(code=fmt[pos + stray.start():pos + stray.start() + 2])
    return None


def _joined(found: list[Word]) -> list[Run] | str:
    if all(isinstance(word, str) for word in found):
        return " ".join(found)  # type: ignore[arg-type]
    out: list[Run] = []
    for index, word in enumerate(found):
        if index:
            out.append(("", " "))
        out.extend([("", word)] if isinstance(word, str) else word)
    return out


def runs(fmt: str, line: list[Word]) -> list[Run]:
    """Format ``fmt`` drawn with ``line``, the words its event passes
    (`words`), as runs. `$N` is word N, `$N-` word N through the end of the
    line, `$*` the whole line; a hole past the line is empty. `%Cnn` turns
    what follows to palette slot nn, `%C` back to the terminal's own colour,
    `%G` is the banner glyph. A hole given runs keeps each run's own colour
    and takes the one in force where a run has none. Trailing spaces are
    trimmed. Never raises: a code it cannot read is drawn as written."""
    current = ""
    out: list[Run] = []

    def add(colour: str, text: str) -> None:
        if text and out and out[-1][0] == colour:
            out[-1] = (colour, out[-1][1] + text)
        elif text:
            out.append((colour, text))

    pos = 0
    for match in _TOKEN.finditer(fmt):
        add(current, fmt[pos:match.start()])
        pos = match.end()
        token = match.group(0)
        if token in ("%%", "$$"):
            add(current, token[0])
        elif token == "%G":
            add(current, banner_text())
        elif token.startswith("%C"):
            slot = match.group(1)
            if slot is None:
                current = ""
            elif int(slot) < PALETTE_SIZE:
                current = hex_for(palette_name(int(slot))) or ""
            else:
                add(current, token)
        else:
            if token == "$*":
                value: Word = _joined(line)
            elif match.group(3):
                value = _joined(line[int(match.group(2)):])
            else:
                index = int(match.group(2))
                value = line[index] if index < len(line) else ""
            for own, text in value if isinstance(value, list) else [("", value)]:
                add(own or current, text)
    add(current, fmt[pos:])
    while out and out[-1][1].rstrip(" ") != out[-1][1]:
        colour, text = out.pop()
        add(colour, text.rstrip(" "))
        if text.strip(" "):
            break
    return out


# ---------------------------------------------------------------------------
# Output targets. Each turns runs into what one target draws; NO_COLOR is
# applied here and in `fg`/`bg`, through `colour_on`, and nowhere else.
# ---------------------------------------------------------------------------

def mark_gap() -> int:
    """The spaces between a speaker's mark and its text: MARK_GAP, or 1 when
    it is not a number."""
    gap = lookup("MARK_GAP") or ""
    return int(gap) if _DIGITS.fullmatch(gap) else 1


def colour_on() -> bool:
    """Whether js draws colour at all: not under NO_COLOR (no-color.org)."""
    return not no_color()


def split_lines(parts: list[Run]) -> list[list[Run]]:
    """Runs cut at each newline into the runs of each line."""
    lines: list[list[Run]] = [[]]
    for colour, text in parts:
        for index, piece in enumerate(text.split("\n")):
            if index:
                lines.append([])
            if piece:
                lines[-1].append((colour, piece))
    return lines


def to_plain(parts: list[Run]) -> str:
    """Runs as plain text: a log, a pipe, a channel that adds its own colour."""
    return "".join(text for _colour, text in parts)


def to_ansi(parts: list[Run]) -> str:
    """Runs as text with 24-bit colour escapes (`38;2;r;g;b`, never a palette
    index), for stdout, stderr and the scrollback."""
    if not colour_on():
        return to_plain(parts)
    return "".join(f"\x1b[38;2;{_rgb(colour)}m{text}{RESET}" if colour else text for colour, text in parts)


def to_fragments(parts: list[Run]) -> list[tuple[str, str]]:
    """Runs as prompt_toolkit (style, text) fragments, for the status bar."""
    on = colour_on()
    return [(colour if on and colour else "", text) for colour, text in parts]


def to_rich(parts: list[Run], style: str = "") -> Any:
    """Runs as a rich `Text` on ``style``, for tool exchanges and the kernel
    panel; a run's own colour wins over ``style``'s."""
    from rich.text import Text

    text = Text(style=style)
    on = colour_on()
    for colour, piece in parts:
        text.append(piece, colour if on and colour else None)
    return text


def render(fmt: str, fields: list[Word], *, colour: bool = True) -> str:
    """`runs` of ``fields`` (`words`) as `to_ansi`, or `to_plain` when
    ``colour`` is False."""
    parts = runs(fmt, words(fields))
    return to_ansi(parts) if colour else to_plain(parts)
