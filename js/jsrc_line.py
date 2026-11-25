"""How one jsrc line reads, typed or in a file: its first word and the rest
(`first_word`), its verb and argument (`split_command`), and a string value
spelled as a JSON string literal (`line_literal`, `from_line_literal`)."""

from __future__ import annotations

import json
import re


def first_word(text: str) -> tuple[str, str]:
    """The first word of ``text`` and the rest after it, stripped, split at
    any run of whitespace: how `set`, `fset`, `on` and every command line,
    typed or in a jsrc, are read."""
    parts = text.strip().split(maxsplit=1)
    if not parts:
        return "", ""
    return parts[0], parts[1].strip() if len(parts) > 1 else ""


def split_command(line: str) -> tuple[str, str] | None:
    """``(verb, arg)`` for a command line, or None for a blank/comment line.
    One leading ``/`` is stripped: it is required at the input line and
    optional everywhere else."""
    body = line.strip()
    if body.startswith("/"):
        body = body[1:].lstrip()
    if not body or body.startswith("#"):
        return None
    verb, arg = first_word(body)
    return verb.lower(), arg


# What a jsrc line cannot hold as it is: a break `str.splitlines` splits a
# file at, or space at either end, which a command strips; nor an empty
# value, which a command reads as none. A value that starts with a quote is
# quoted too, so it never reads as a literal.
_UNHELD = re.compile("[\n\r\v\f\x1c\x1d\x1e\x85  ]|^\\s|\\s$|^\"")


def line_literal(text: str) -> str:
    """``text`` as one jsrc line holds it: as it is, or, when a line cannot
    hold it as it is, a JSON string literal, every break in it escaped."""
    if text and not _UNHELD.search(text):
        return text
    quoted = json.dumps(text, ensure_ascii=False)
    for char in "\x85  ":
        quoted = quoted.replace(char, f"\\u{ord(char):04x}")
    return quoted


def from_line_literal(text: str) -> str:
    """A string value as a jsrc line or a typed command gives it: a JSON
    string literal is the string it spells (`line_literal`), anything else
    is itself."""
    if len(text) >= 2 and text.startswith('"') and text.endswith('"'):
        try:
            value = json.loads(text)
        except ValueError:
            return text
        if isinstance(value, str):
            return value
    return text
