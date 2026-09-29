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

Every record of a session file carries an `id`, eight hex digits unique within
the file, and a `parent`. The message and mark records form the conversation
path: each one's parent is the id of the message or mark before it in the file,
null for the first. A start or title record's parent is the message or mark it
was written after; nothing names it as parent. `id` and `parent` are the first
two keys of every record `append` writes. A branch is a new file whose copied
records keep their ids, so the parent's message it split at is named by id.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import messages as msgs
from . import paths

SUFFIX = ".jsonl"
TEXT_SUFFIX = ".txt"
_GENERATED = (
    re.compile(r"\d{4}-\d{2}-\d{2}T\d{4}-[0-9a-f]{4}"),
    # Before this layout: UTC timestamp to the microsecond and 16 hex digits.
    re.compile(r"\d{8}T\d{12}Z-[0-9a-f]{16}"),
)
_TASK = re.compile(r"task-\d+-[0-9a-f]{4}")
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


def is_named(stem: str) -> bool:
    """Whether a session file's stem is a name someone gave it (`--session
    NAME`): neither a generated name nor a subagent run's task name."""
    return bool(stem) and not is_generated(stem) and _TASK.fullmatch(stem) is None


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


def branch_parent(metadata: Any) -> tuple[str, Any] | None:
    """The parent session file and branch point a start record names, or None
    when the record is not a branch's start.

    The point is the `message` of the record's `branched_from`: the id of the
    message record `js.session_catalog.branch_session` split at."""
    branch = metadata.get("branched_from") if isinstance(metadata, dict) else None
    if not isinstance(branch, dict) or not isinstance(branch.get("session"), str):
        return None
    return branch["session"], branch.get("message")


def point_label(number: int) -> str:
    """A message number as the transcript and the picker show it: `#0031`."""
    return f"#{number:04d}"


def display_name(session_file: Path, root: Path | None = None) -> str:
    """The name `--session` takes for this file: its path under its folder, no suffix."""
    try:
        relative = Path(session_file).relative_to(paths.sessions_root() if root is None else root)
    except ValueError:
        return str(session_file)
    parts = relative.with_suffix("").parts
    return "/".join(parts[1:] if len(parts) > 1 else parts)


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


# --- record ids ------------------------------------------------------------------

_ID_BYTES = 4
_PATH_KINDS = frozenset({"message", "mark"})
_HEAD = re.compile(rb'^\{"id":"([^"]+)","parent":(?:null|"[^"]*"),"(kind|role)":"([^"]*)"')
_CACHED_CHAINS = 64


def on_path(record: dict) -> bool:
    """Whether a record is on the conversation path: a message or a mark, or a
    bare message of a session that predates the record envelope."""
    kind = record.get("kind")
    return kind in _PATH_KINDS or (kind is None and "role" in record)


def new_id(taken: set[str]) -> str:
    while True:
        candidate = secrets.token_hex(_ID_BYTES)
        if candidate not in taken:
            return candidate


@dataclass
class _Chain:
    """The ids of one session file and the id of its last path record."""

    device: int
    inode: int
    offset: int = 0
    taken: set[str] = field(default_factory=set)
    leaf: str | None = None

    def feed(self, line: bytes) -> None:
        head = _HEAD.match(line)
        if head is not None:
            record_id = head.group(1).decode("utf-8", errors="replace")
            path = head.group(2) == b"role" or head.group(3).decode("utf-8", errors="replace") in _PATH_KINDS
        elif b'"id"' in line:
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                return
            if not isinstance(record, dict) or not isinstance(record.get("id"), str):
                return
            record_id, path = record["id"], on_path(record)
        else:
            return
        self.taken.add(record_id)
        if path:
            self.leaf = record_id


_chains_lock = threading.Lock()
_chains: OrderedDict[str, _Chain] = OrderedDict()


def _chain(key: str, fd: int) -> tuple[_Chain, bool]:
    """The chain of the file open at `fd`, brought up to its end, and whether
    the file ends inside a line."""
    info = os.fstat(fd)
    chain = _chains.pop(key, None)
    if chain is None or (chain.device, chain.inode) != (info.st_dev, info.st_ino) or info.st_size < chain.offset:
        chain = _Chain(info.st_dev, info.st_ino)
    data = os.pread(fd, info.st_size - chain.offset, chain.offset) if info.st_size > chain.offset else b""
    end = data.rfind(b"\n") + 1
    for line in data[:end].splitlines():
        chain.feed(line)
    chain.offset += end
    _chains[key] = chain
    while len(_chains) > _CACHED_CHAINS:
        _chains.popitem(last=False)
    return chain, end < len(data)


def _encode(record: dict) -> bytes:
    return (json.dumps(record, separators=(",", ":"), default=str) + "\n").encode("utf-8")


def _linked(record: dict, record_id: str, parent: str | None) -> dict:
    return {"id": record_id, "parent": parent, **{k: v for k, v in record.items() if k not in ("id", "parent")}}


def append(session_file: Path, record: dict[str, Any]) -> dict[str, Any]:
    """Append `record` to `session_file` under an exclusive lock, with a fresh
    id and its parent, and fsync. Returns the record as written. A record for
    os.devnull, the file of a session that is not saved, is discarded."""
    session_file = Path(session_file)
    if session_file == Path(os.devnull):
        return dict(record)
    session_file.parent.mkdir(parents=True, exist_ok=True)
    key = str(session_file.resolve(strict=False))
    with open(session_file, "a+b") as stream:
        fd = stream.fileno()
        fcntl.flock(fd, fcntl.LOCK_EX)
        with _chains_lock:
            chain, torn = _chain(key, fd)
            linked = _linked(record, new_id(chain.taken), chain.leaf)
            # A line a crashed writer left unfinished is closed first, so this
            # record does not run into it.
            data = (b"\n" if torn else b"") + _encode(linked)
            stream.write(data)
            stream.flush()
            os.fsync(fd)
            chain.taken.add(linked["id"])
            if on_path(linked):
                chain.leaf = linked["id"]
            chain.offset = os.fstat(fd).st_size
    return linked


def link_lines(lines: Iterable[str]) -> tuple[list[str], int]:
    """`lines` of a session file with an id and a parent given to every record
    that has none, and how many were given one. A line that is not a JSON
    object is kept as it is."""
    taken: set[str] = set()
    records: list[tuple[str, dict | None]] = []
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            record = None
        record = record if isinstance(record, dict) else None
        if record is not None and isinstance(record.get("id"), str):
            taken.add(record["id"])
        records.append((line, record))
    out: list[str] = []
    leaf: str | None = None
    given = 0
    for line, record in records:
        if record is None:
            out.append(line if line.endswith("\n") else line + "\n")
            continue
        if isinstance(record.get("id"), str):
            out.append(line if line.endswith("\n") else line + "\n")
        else:
            record = _linked(record, new_id(taken), leaf)
            taken.add(record["id"])
            given += 1
            out.append(_encode(record).decode("utf-8"))
        if on_path(record):
            leaf = record["id"]
    return out, given


def link_file(session_file: Path) -> int:
    """Rewrite `session_file` once with an id and a parent on every record that
    has none, keeping its modification time. Returns how many records were
    given one; with none to give, the file is left untouched."""
    session_file = Path(session_file)
    with open(session_file, "r+", encoding="utf-8", errors="surrogateescape") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        info = os.fstat(stream.fileno())
        lines, given = link_lines(stream.read().splitlines(keepends=True))
        if not given:
            return 0
        staging = session_file.with_name(f".{session_file.name}.{secrets.token_hex(4)}.tmp")
        with open(staging, "w", encoding="utf-8", errors="surrogateescape") as out:
            out.writelines(lines)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(staging, info.st_mode & 0o7777)
        os.utime(staging, ns=(info.st_atime_ns, info.st_mtime_ns))
        os.replace(staging, session_file)
    return given
