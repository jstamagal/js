"""Overridable model instructions with single-pass named substitutions."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import paths


class SnippetError(ValueError):
    """Required model instruction configuration could not be loaded."""


_HOLE = re.compile(r"\{\{([A-Z][A-Z0-9_]*)\}\}")


def substitute(text: str, values: dict[str, Any]) -> str:
    """Insert values once; inserted text stays literal."""
    return _HOLE.sub(lambda match: str(values[match[1]]), text)


def render(name: str, **values: Any) -> str:
    """Read a required snippet, preserving its whitespace and inserted values."""
    relative = f"snippets/{name}.md"
    searched = paths.searched(relative)
    try:
        path = paths.find(relative)
        if path is None:
            raise FileNotFoundError(relative)
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise SnippetError(f"cannot read snippet {name!r}; searched: {searched}: {exc}") from exc
    try:
        return substitute(text, values)
    except KeyError as exc:
        raise SnippetError(f"snippet {name!r} needs {{{{{exc.args[0]}}}}}; searched: {searched}") from exc
