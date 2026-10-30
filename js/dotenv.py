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

from . import paths as _paths, settings as _settings

FILENAME = ".env"


def _unquote(value: str) -> str:
    if value[:1] in ("'", '"'):
        for index in range(1, len(value)):
            if value[index] == value[0]:
                tail = value[index + 1:].lstrip()
                if not tail or tail.startswith("#"):
                    return value[1:index]
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


def candidate_files(cwd: Path | None = None, *, settings: dict | None = None) -> list[Path]:
    """`.env` files to consult, highest precedence first.

    `cwd/.env` first, then each parent up to the filesystem root, then the
    global `~/.js/.env` last.  `load()` fills with setdefault, so the
    nearest file wins and the global one only supplies what nothing else did.
    """
    start = (cwd or Path.cwd()).resolve(strict=False)
    directories = (start, *start.parents) if _settings.knob(settings, "DOTENV_WALK_UP") else (start,)
    files: list[Path] = [parent / FILENAME for parent in directories]
    files.append(_paths.global_env_file())
    seen: set[Path] = set()
    return [path for path in files if not (path in seen or seen.add(path))]


def load(cwd: Path | None = None, environ: dict[str, str] | None = None, *, settings: dict | None = None) -> list[Path]:
    """Fill unset env names from `.env` files. Returns the files that applied."""
    if settings is None:
        from . import config

        try:
            settings = config.run_settings(cwd, held=True)
        except ValueError:
            # A bad setting is reported where startup loads settings; the
            # .env files load with the shipped DOTENV_WALK_UP.
            settings = None
    env =os.environ if environ is None else environ
    applied: list[Path] = []
    for path in candidate_files(cwd, settings=settings):
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
