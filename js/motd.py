"""The message of the day: art printed once when the REPL starts.

`MOTD_FILE` names one file of entries in fortune(6) form, split by lines
holding only `%`; one entry is picked at random. `MOTD_DIR` names a directory
of art files (TheDraw `.ANS` and the like); one file is picked at random.
`MOTD_FILE` wins when both are set; neither set is no MOTD.

The text goes into the REPL's scrollback with the startup line, escape codes
and all: colours show there, cursor moves do not. It decodes as UTF-8 when it
can, else as CP437, the code page TheDraw art is drawn in. A SAUCE record,
everything from the 0x1A end-of-file byte on, is dropped.
"""

from __future__ import annotations

import random
import re
from pathlib import Path

# Ends what the art drew, so its colours do not run on into js's own output.
RESET = "\x1b[0m"


def decode(data: bytes) -> str:
    """``data`` as text: everything before a 0x1A byte (the SAUCE record
    follows it), UTF-8 when it decodes, else CP437."""
    data = data.split(b"\x1a", 1)[0]
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp437")


def entries(text: str) -> list[str]:
    """The fortune(6) entries of ``text``: the runs of lines between lines
    holding only `%`. Empty entries are dropped."""
    found: list[str] = []
    current: list[str] = []
    # Split on newlines only: art holds form feeds and other bytes
    # str.splitlines would also break on.
    parts = text.split("\n")
    for line in [part + "\n" for part in parts[:-1]] + [parts[-1]]:
        if line.rstrip("\r\n") == "%":
            found.append("".join(current))
            current = []
        else:
            current.append(line)
    found.append("".join(current))
    return [entry for entry in found if entry.strip()]


def pick(motd_file: str | None, motd_dir: str | None, rng: random.Random | None = None) -> str | None:
    """One MOTD, or None when neither setting names something readable."""
    choose = (rng or random).choice
    if motd_file:
        try:
            text = decode(Path(motd_file).expanduser().read_bytes())
        except OSError:
            return None
        found = entries(text)
        return choose(found) if found else None
    if motd_dir:
        try:
            files = sorted(path for path in Path(motd_dir).expanduser().iterdir() if path.is_file())
        except OSError:
            return None
        if not files:
            return None
        try:
            return decode(choose(files).read_bytes())
        except OSError:
            return None
    return None


_SGR = re.compile(r"\x1b\[([0-9;]*)m")
# SGR attribute codes and the code that turns each off.
_ATTRIBUTE_OFF = {1: 22, 2: 22, 3: 23, 4: 24, 5: 25, 6: 25, 7: 27, 8: 28, 9: 29}


class _Style:
    """The SGR state in force: attributes on, foreground, background."""

    def __init__(self) -> None:
        self.attributes: set[int] = set()
        self.fg: str = ""
        self.bg: str = ""

    def apply(self, params: str) -> None:
        codes = [int(code) if code else 0 for code in params.split(";")] if params else [0]
        i = 0
        while i < len(codes):
            code = codes[i]
            if code == 0:
                self.attributes, self.fg, self.bg = set(), "", ""
            elif code in _ATTRIBUTE_OFF:
                self.attributes.add(code)
            elif code in _ATTRIBUTE_OFF.values():
                self.attributes -= {on for on, off in _ATTRIBUTE_OFF.items() if off == code}
            elif code in (38, 48):
                width = 3 if codes[i + 1:i + 2] == [5] else 5 if codes[i + 1:i + 2] == [2] else 1
                colour = ";".join(str(c) for c in codes[i:i + width])
                if code == 38:
                    self.fg = colour
                else:
                    self.bg = colour
                i += width
                continue
            elif 30 <= code <= 37 or 90 <= code <= 97:
                self.fg = str(code)
            elif code == 39:
                self.fg = ""
            elif 40 <= code <= 47 or 100 <= code <= 107:
                self.bg = str(code)
            elif code == 49:
                self.bg = ""
            i += 1

    def sequence(self) -> str:
        """One SGR sequence that sets this state, or "" for the default."""
        params = [str(code) for code in sorted(self.attributes)] + [part for part in (self.fg, self.bg) if part]
        return f"\x1b[{';'.join(params)}m" if params else ""


def lines_for_scrollback(text: str) -> str:
    """``text`` as the REPL's scrollback shows it: line ends made `\\n` (CRLF
    and a lone CR, as DOS and old Mac art has them), and each line started
    with one SGR sequence for the colours and attributes still in force from
    the lines above, since the scrollback reads every line on its own."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    style = _Style()
    out: list[str] = []
    for line in text.split("\n"):
        out.append(style.sequence() + line)
        for match in _SGR.finditer(line):
            style.apply(match.group(1))
    return "\n".join(out)
