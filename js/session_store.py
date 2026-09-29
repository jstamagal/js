"""Where session files live and how a name finds one.

Sessions are filed by the directory they were started in:

    ~/.js/sessions/<slug>/<name>.jsonl     the record, append-only
    ~/.js/sessions/<slug>/<name>.txt       its readable transcript (js.session_text)
    ~/.js/sessions/<slug>/<name>/          that session's subagent runs

The slug is the start directory's absolute path with `/` and `_` replaced by
`-`, so every folder name starts with `-`. A generated name is
`YYYY-MM-DDTHHMM-xxxx` in local time; `ls` order is time order.

A name resolves in the current directory's folder first, then in every folder.
A name found in more than one other folder is refused with the paths. A
generated name also resolves from a unique tail of at least four characters
(`6d65`), the same way.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from . import messages as msgs
from . import paths

SUFFIX = ".jsonl"
TEXT_SUFFIX = ".txt"
_GENERATED = (
    re.compile(r"\d{4}-\d{2}-\d{2}T\d{4}-[0-9a-f]{4}"),
    # Before this layout: UTC timestamp to the microsecond and 16 hex digits.
    re.compile(r"\d{8}T\d{12}Z-[0-9a-f]{16}"),
)
_TAIL_MIN = 4
_RESERVE_TRIES = 64


class AmbiguousSession(ValueError):
    """A session name that more than one folder holds."""

    def __init__(self, session: str, found: list[Path]) -> None:
        self.found = found
        super().__init__(msgs.SESSION_AMBIGUOUS.text(session=session, paths=", ".join(str(p) for p in found)))


def slug(directory: Path | str) -> str:
    """The folder name for sessions started in `directory`."""
    absolute = str(Path(directory).expanduser().resolve(strict=False))
    return absolute.replace("/", "-").replace("_", "-")


def folder_for(directory: Path | str) -> Path:
    return paths.sessions_root() / slug(directory)


def is_folder_name(name: str) -> bool:
    """Whether a sessions/ entry name is a start-directory folder."""
    return name.startswith("-")


def folders() -> list[Path]:
    root = paths.sessions_root()
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return []
    return [entry for entry in entries if is_folder_name(entry.name) and entry.is_dir()]


def generated_name(now: float | None = None) -> str:
    stamp = datetime.fromtimestamp(time.time() if now is None else now)
    return f"{stamp:%Y-%m-%dT%H%M}-{secrets.token_hex(2)}"


def task_name(now: float | None = None) -> str:
    return f"task-{int(time.time() if now is None else now)}-{secrets.token_hex(2)}"


def is_generated(stem: str) -> bool:
    return any(pattern.fullmatch(stem) for pattern in _GENERATED)


def subagent_folder(parent_file: Path) -> Path:
    """Where the subagent runs of the session in `parent_file` are filed."""
    return parent_file.with_suffix("")


def text_path(session_file: Path) -> Path:
    return session_file.with_suffix(TEXT_SUFFIX)


def reserve(folder: Path, make_name: Callable[[], str] = generated_name) -> Path:
    """Create a new empty session file in `folder` under a fresh name."""
    folder.mkdir(parents=True, exist_ok=True)
    for _ in range(_RESERVE_TRIES):
        path = folder / f"{make_name()}{SUFFIX}"
        try:
            with path.open("x", encoding="utf-8"):
                pass
        except FileExistsError:
            continue
        return path
    raise RuntimeError(msgs.SESSION_NOT_RESERVED.text(folder=folder))


def _named(folder: Path, relative: Path) -> Path | None:
    candidate = folder / relative
    return candidate if candidate.is_file() else None


def _tails(folder: Path, tail: str) -> list[Path]:
    try:
        entries = sorted(folder.glob(f"*{SUFFIX}"))
    except OSError:
        return []
    return [entry for entry in entries
            if entry.stem.endswith(tail) and is_generated(entry.stem) and entry.is_file()]


def _one(session: str, found: list[Path]) -> Path | None:
    if len(found) > 1:
        raise AmbiguousSession(session, found)
    return found[0] if found else None


def find(session: str, relative: Path, current: Path) -> Path | None:
    """The existing session a validated relative name names, or None.

    Exact names first, in `current` and then in every folder; then, for a name
    of one component, a unique tail of a generated name, in the same order."""
    found = _named(current, relative)
    if found is not None:
        return found
    others = [folder for folder in folders() if folder != current]
    found = _one(session, [path for folder in others if (path := _named(folder, relative)) is not None])
    if found is not None:
        return found
    tail = relative.with_suffix("").as_posix()
    if "/" in tail or len(tail) < _TAIL_MIN:
        return None
    found = _one(session, _tails(current, tail))
    if found is not None:
        return found
    return _one(session, [path for folder in others for path in _tails(folder, tail)])


def display_name(session_file: Path) -> str:
    """The name `--session` takes for this file: its path under its folder, no suffix."""
    root = paths.sessions_root()
    try:
        relative = session_file.relative_to(root)
    except ValueError:
        return str(session_file)
    parts = relative.with_suffix("").parts
    return "/".join(parts[1:]) if len(parts) > 1 and is_folder_name(parts[0]) else "/".join(parts)


def write_latest(agent_state_dir: Path, session_file: Path) -> None:
    """Record `session_file` as this agent's most recent session, for --last."""
    agent_state_dir.mkdir(parents=True, exist_ok=True)
    tmp = agent_state_dir / f".latest.{secrets.token_hex(6)}.tmp"
    tmp.write_text(
        json.dumps({"session_file": str(session_file), "session_name": display_name(session_file)},
                   separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, agent_state_dir / "latest.json")


def read_latest(agent_state_dir: Path) -> Path | None:
    """This agent's most recent session file, or None when it is gone."""
    try:
        payload = json.loads((agent_state_dir / "latest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    session_file = payload.get("session_file") if isinstance(payload, dict) else None
    if not isinstance(session_file, str) or not Path(session_file).is_file():
        return None
    return Path(session_file)
