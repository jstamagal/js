"""`.env` loading for js.

Tool keys (`TAVILY_API_KEY`, `EXA_API_KEY`, `SERPER_API_KEY`, ...) are read
straight from `os.environ` at call time.  Launching through `just run` picked
those up from a `.env` because the justfile sets `dotenv-load`; a bare `js` on
PATH did not.  This module closes that gap so both entry points see the same
keys.

Precedence: the real process environment always wins.  A `.env` only fills
names that are unset, so `TAVILY_API_KEY=x js ...` still beats the file.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import paths as _paths

FILENAME = ".env"


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    # Unquoted values stop at an inline comment, matching common .env readers.
    head, sep, _ = value.partition(" #")
    return (head if sep else value).strip()


def parse(text: str) -> dict[str, str]:
    """Parse `.env` text into a mapping. Malformed lines are skipped."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        name, sep, value = line.partition("=")
        name = name.strip()
        if not sep or not name:
            continue
        out[name] = _unquote(value.strip())
    return out


def candidate_files(cwd: Path | None = None) -> list[Path]:
    """`.env` files to consult, highest precedence first.

    `cwd/.env` first, then each parent up to the filesystem root, then the
    global `~/.config/js/.env` last.  `load()` fills with setdefault, so the
    nearest file wins and the global one only supplies what nothing else did.
    """
    start = (cwd or Path.cwd()).resolve(strict=False)
    files: list[Path] = [parent / FILENAME for parent in (start, *start.parents)]
    files.append(_paths.config_dir() / FILENAME)
    seen: set[Path] = set()
    return [path for path in files if not (path in seen or seen.add(path))]


def load(cwd: Path | None = None, environ: dict[str, str] | None = None) -> list[Path]:
    """Fill unset env names from `.env` files. Returns the files that applied."""
    env = os.environ if environ is None else environ
    applied: list[Path] = []
    for path in candidate_files(cwd):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        values = parse(text)
        if not values:
            continue
        applied.append(path)
        for name, value in values.items():
            env.setdefault(name, value)
    return applied
