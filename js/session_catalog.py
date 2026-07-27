"""Session discovery, start metadata, and process-backed liveness.

Conversation files remain append-only JSONL.  Session metadata is an ignored
control record in that same stream; open-process state lives in adjacent hidden
sidecars so it can be updated without touching conversation history.
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

_METADATA_KIND = "session_metadata"
_METADATA_VERSION = 1
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


def record_session_start(
    session_file: Path,
    *,
    cwd: Path | str,
    caller_key: str | None = None,
    job_id: str | int | None = None,
) -> None:
    """Append non-message session start metadata to a conversation JSONL file."""
    session_file = Path(session_file)
    session_file.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "kind": _METADATA_KIND,
        "version": _METADATA_VERSION,
        "ts": time.time(),
        "cwd": str(Path(cwd).expanduser().resolve(strict=False)),
        "caller_key": caller_key,
        "job_id": job_id,
    }
    with session_file.open("a", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        stream.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


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


def catalog_sessions(sessions_root: Path) -> list[dict[str, Any]]:
    """Recursively catalog all agent session JSONL files under *sessions_root*."""
    root = Path(sessions_root)
    records: list[dict[str, Any]] = []
    if not root.is_dir():
        return records
    for agent_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        for path in sorted(agent_dir.rglob("*.jsonl")):
            if not path.is_file():
                continue
            stat = path.stat()
            user_turns, metadata = _session_details(path)
            relative = path.relative_to(agent_dir).with_suffix("").as_posix()
            records.append(
                {
                    "agent": agent_dir.name,
                    "name": relative,
                    "path": str(path),
                    "mtime": stat.st_mtime,
                    "size": stat.st_size,
                    "user_turns": user_turns,
                    "in_flight": session_in_flight(path),
                    "cwd": metadata.get("cwd") if metadata else None,
                    "caller_key": metadata.get("caller_key") if metadata else None,
                    "job_id": metadata.get("job_id") if metadata else None,
                }
            )
    return records


# Explicitly named aliases make the lifecycle API easy to discover at call sites.
acquire_session_liveness = acquire_session
release_session_liveness = release_session
list_sessions = catalog_sessions
