"""Session discovery, start metadata, titles, branches, and process-backed liveness.

Conversation files remain append-only JSONL.  Session metadata is an ignored
control record in that same stream, one per start: the directory, agent and
model, how it was started (`repl`, `-p`, `pipe`, `subagent`, `commit`) and the
command line; a subagent run names its parent session, a branch its parent
session and the id of the message it split at. `/name` appends a title record;
`/model` appends a model_switch record with the stamp it switched to and the
one it left.
Every record carries an `id` and a `parent` (`js.session_store`). Open-process
state lives in adjacent hidden sidecars so it can be updated without touching
conversation history.
"""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import messages as msgs
from . import session_store
from . import session_text

_METADATA_KIND = "session_metadata"
_METADATA_VERSION = 3
_TITLE_KIND = "title"
_MODEL_SWITCH_KIND = "model_switch"
_LIVENESS_VERSION = 1


@dataclass(frozen=True)
class SessionLease:
    """One independently releasable open of a session by a process."""

    session_file: Path
    token: str
    pid: int
    process_start: str | None

    def release(self) -> None:
        release_session(self)

    def __enter__(self) -> SessionLease:
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.release()


def _sidecar_paths(session_file: Path) -> tuple[Path, Path]:
    session_file = Path(session_file)
    stem = f".{session_file.name}.liveness"
    return session_file.parent / f"{stem}.json", session_file.parent / f"{stem}.lock"


def _process_status(pid: int) -> tuple[str, str] | None:
    """Return Linux's process state and start tick when procfs is available."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        fields = raw[raw.rfind(")") + 2 :].split()
        return fields[0], fields[19]
    except (OSError, IndexError):
        return None


def _process_start(pid: int) -> str | None:
    """Return Linux's process start tick, when available, to defeat PID reuse."""
    status = _process_status(pid)
    return status[1] if status is not None else None


def _pid_alive(pid: int, process_start: str | None) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    except OSError:
        return False
    status = _process_status(pid)
    if status is not None:
        state, current_start = status
        if state in {"Z", "X", "x"}:
            return False
        if process_start is not None and current_start != process_start:
            return False
    return True


def _read_liveness(path: Path) -> list[dict[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, dict) or raw.get("version") != _LIVENESS_VERSION:
        return []
    opens = raw.get("opens")
    if not isinstance(opens, list):
        return []
    return [item for item in opens if isinstance(item, dict)]


def _write_liveness(path: Path, opens: list[dict[str, Any]]) -> None:
    if not opens:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{secrets.token_hex(6)}.tmp"
    temporary.write_text(
        json.dumps({"version": _LIVENESS_VERSION, "opens": opens}, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _with_liveness_lock(session_file: Path, operation):
    state_path, lock_path = _sidecar_paths(session_file)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        opens = _read_liveness(state_path)
        result, updated = operation(opens)
        if updated is not None:
            _write_liveness(state_path, updated)
        return result


def acquire_session(session_file: Path, *, pid: int | None = None) -> SessionLease:
    """Record one open of *session_file* and return its release handle."""
    session_file = Path(session_file).resolve(strict=False)
    owner_pid = os.getpid() if pid is None else int(pid)
    lease = SessionLease(session_file, secrets.token_hex(16), owner_pid, _process_start(owner_pid))

    def add(opens):
        live = [
            item
            for item in opens
            if isinstance(item.get("pid"), int)
            and _pid_alive(item["pid"], item.get("process_start"))
        ]
        live.append(
            {
                "token": lease.token,
                "pid": lease.pid,
                "process_start": lease.process_start,
                "acquired_at": time.time(),
            }
        )
        return lease, live

    return _with_liveness_lock(session_file, add)


def release_session(lease: SessionLease) -> None:
    """Release exactly one acquired open, preserving concurrent opens."""

    def remove(opens):
        return None, [item for item in opens if item.get("token") != lease.token]

    _with_liveness_lock(lease.session_file, remove)


def session_in_flight(session_file: Path) -> bool:
    """Return true only when at least one recorded opener is still alive."""

    def inspect(opens):
        live = [
            item
            for item in opens
            if isinstance(item.get("pid"), int)
            and _pid_alive(item["pid"], item.get("process_start"))
        ]
        return bool(live), live if live != opens else None

    return _with_liveness_lock(Path(session_file).resolve(strict=False), inspect)


def _append_record(session_file: Path, record: dict[str, Any]) -> None:
    session_store.append(session_file, record)
    session_text.refresh(session_file)


def record_session_start(
    session_file: Path,
    *,
    cwd: Path | str,
    caller_key: str | None = None,
    job_id: str | int | None = None,
    agent: str | None = None,
    model: str | None = None,
    mode: str | None = None,
    command: list[str] | None = None,
    parent: Path | str | None = None,
    branched_from: dict[str, Any] | None = None,
    ts: float | None = None,
) -> None:
    """Append non-message session start metadata to a conversation JSONL file.

    *agent* and *model* are recorded so a later resume with no flags can come
    back on what was actually in use rather than the config default. *parent*
    is the session file a subagent run belongs to, recorded as `parent_session`;
    *branched_from* is `{"session": <parent path>, "message": <message id>}`."""
    record = {
        "kind": _METADATA_KIND,
        "version": _METADATA_VERSION,
        "ts": time.time() if ts is None else ts,
        "cwd": str(Path(cwd).expanduser().resolve(strict=False)),
        "caller_key": caller_key,
        "job_id": job_id,
        "agent": agent,
        "model": model,
    }
    if mode is not None:
        record["mode"] = mode
    if command is not None:
        record["command"] = list(command)
    if parent is not None:
        record["parent_session"] = str(parent)
    if branched_from is not None:
        record["branched_from"] = branched_from
    _append_record(session_file, record)


def append_title(session_file: Path, title: str) -> None:
    """Pin `title` as the session's name."""
    _append_record(session_file, {"kind": _TITLE_KIND, "ts": time.time(), "title": title})


def record_model_switch(session_file: Path, *, stamp: dict[str, Any], previous: dict[str, Any]) -> None:
    """Record a `/model` switch: `stamp` is what the session runs on now and
    `previous` what it ran on before, each shaped as an assistant message's
    stamp (model, provider, reasoning)."""
    _append_record(session_file, {"kind": _MODEL_SWITCH_KIND, "ts": time.time(),
                                  "stamp": dict(stamp), "previous": dict(previous)})


def _records(path: Path):
    try:
        with path.open(encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_SH)
            lines = stream.readlines()
    except OSError:
        return
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            yield line, record


def session_title(session_file: Path) -> str | None:
    """The newest `/name` title of a session, or None."""
    title = None
    for _, record in _records(Path(session_file)):
        if record.get("kind") == _TITLE_KIND and isinstance(record.get("title"), str):
            title = record["title"]
    return title


def first_metadata(session_file: Path) -> dict[str, Any] | None:
    """The metadata of the session's first start, or None."""
    for _, record in _records(Path(session_file)):
        if record.get("kind") == _METADATA_KIND:
            return record
    return None


def last_stamp(session_file: Path) -> dict[str, Any] | None:
    """The newest model record of a session: an assistant message's stamp, a
    `/model` switch's stamp, or a start's model, whichever was written last.
    None when none exists."""
    last = None
    for _, record in _records(Path(session_file)):
        kind = record.get("kind")
        if kind in ("message", _MODEL_SWITCH_KIND) and isinstance(record.get("stamp"), dict):
            stamp = record["stamp"]
            if isinstance(stamp.get("model"), str) and stamp["model"]:
                last = stamp
        elif kind == _METADATA_KIND and isinstance(record.get("model"), str) and record["model"]:
            last = {"model": record["model"]}
    return last


def branch_session(parent_file: Path, message: str, *, cwd: Path | str, agent: str | None = None,
                   mode: str | None = None, command: list[str] | None = None) -> Path:
    """A new session in the parent's folder holding the parent's records up to
    and including the message record whose id is `message`, with its branch
    point recorded. The copied records keep their ids."""
    parent_file = Path(parent_file)
    kept: list[str] = []
    found = False
    for line, record in _records(parent_file):
        if record.get("kind") in (_METADATA_KIND, _TITLE_KIND):
            continue
        kept.append(line if line.endswith("\n") else line + "\n")
        if record.get("id") == message and session_store.on_path(record) and record.get("kind") != "mark":
            found = True
            break
    if not found:
        raise ValueError(msgs.SESSION_BRANCH_NO_MESSAGE.text(path=parent_file, message=message))
    branch = session_store.reserve(parent_file.parent)
    with branch.open("a", encoding="utf-8") as stream:
        stream.writelines(kept)
    stamp = last_stamp(branch)
    record_session_start(branch, cwd=cwd, agent=agent, model=stamp.get("model") if stamp else None,
                         mode=mode, command=command,
                         branched_from={"session": str(parent_file), "message": message})
    return branch


def _session_details(path: Path) -> tuple[int, dict[str, Any] | None]:
    user_turns = 0
    metadata = None
    try:
        with path.open(encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_SH)
            for line in stream:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                if record.get("kind") == "message":
                    message = record.get("message")
                    if isinstance(message, dict) and message.get("role") == "user":
                        user_turns += 1
                elif record.get("role") == "user":
                    # Sessions predating the append-only envelope stored messages
                    # directly. Keep their turn counts useful in the catalog.
                    user_turns += 1
                elif record.get("kind") == _METADATA_KIND:
                    metadata = record
    except OSError:
        pass
    return user_turns, metadata


def last_session_model(session_file: Path) -> str | None:
    """The model of the session's last stamp, or None when unknown.

    Sessions written before models were recorded, and those whose metadata is
    unreadable, resolve to None so the caller falls back to its own default."""
    stamp = last_stamp(Path(session_file))
    return stamp.get("model") if stamp else None


def _session_files(root: Path):
    for path in sorted(root.rglob(f"*{session_store.SUFFIX}")):
        if path.is_file() and not path.name.startswith("."):
            yield path


def catalog_sessions(sessions_root: Path) -> list[dict[str, Any]]:
    """Catalog every session JSONL file under *sessions_root*, subagent runs included."""
    root = Path(sessions_root)
    records: list[dict[str, Any]] = []
    if not root.is_dir():
        return records
    for path in _session_files(root):
        stat = path.stat()
        user_turns, metadata = _session_details(path)
        folder = path.relative_to(root).parts[0]
        agent = metadata.get("agent") if metadata else None
        if agent is None and not session_store.is_folder_name(folder):
            agent = folder
        records.append(
            {
                "agent": agent,
                "name": session_store.display_name(path, root),
                "folder": folder,
                "path": str(path),
                "mtime": stat.st_mtime,
                "size": stat.st_size,
                "user_turns": user_turns,
                "in_flight": session_in_flight(path),
                "cwd": metadata.get("cwd") if metadata else None,
                "caller_key": metadata.get("caller_key") if metadata else None,
                "job_id": metadata.get("job_id") if metadata else None,
                "model": metadata.get("model") if metadata else None,
                "mode": metadata.get("mode") if metadata else None,
                "title": session_title(path),
            }
        )
    return records


# Explicitly named aliases make the lifecycle API easy to discover at call sites.
acquire_session_liveness = acquire_session
release_session_liveness = release_session
list_sessions = catalog_sessions
