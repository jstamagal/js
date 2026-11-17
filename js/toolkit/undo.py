"""Undo snapshots: the state of each path before a tool changed it, newest
last, kept in memory and, for a saved session, in a bounded store on disk so
js restarted on the session can still undo."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import stat
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .. import settings as _settings
from ..paths import state_root
from .reads import Coverage, atomic_write


Snapshot = bytes | None | dict[str, Any]

_SNAPSHOT_FORMAT_VERSION = 1
_SNAPSHOT_MAX_ENTRIES = 100
_SNAPSHOT_MAX_DISK_BYTES = 64 * 1024 * 1024


def session_directory(agent_id: str, session_file: Path, *, state_dir: Path | None = None) -> Path | None:
    """The undo store of ``agent_id``'s session ``session_file`` under
    ``state_dir`` (the state root by default); None for a session that is not
    saved."""
    session = Path(session_file).expanduser().resolve(strict=False)
    if session == Path(os.devnull).resolve(strict=False):
        return None
    safe_agent = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in agent_id) or _settings.default_value("DEFAULT_AGENT")
    safe_stem = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in session.stem) or "session"
    session_hash = hashlib.sha256(str(session).encode("utf-8")).hexdigest()[:16]
    return (state_dir or state_root()) / safe_agent / "undo" / f"{safe_stem}-{session_hash}"


def _encode_snapshot(path: Path, snapshot: Snapshot) -> bytes:
    if snapshot is None:
        payload: dict[str, Any] = {"kind": "missing"}
    elif isinstance(snapshot, bytes):
        payload = {"kind": "file", "data": base64.b64encode(snapshot).decode("ascii")}
    elif snapshot.get("kind") == "file":
        payload = {**snapshot, "data": base64.b64encode(snapshot["data"]).decode("ascii")}
    elif snapshot.get("kind") == "symlink":
        payload = {"kind": "symlink", "target": str(snapshot.get("target", ""))}
    elif snapshot.get("kind") == "unavailable":
        payload = {"kind": "unavailable", "reason": str(snapshot["reason"])}
    elif snapshot.get("kind") == "directory":
        entries = []
        for rel, data in snapshot.get("entries", {}).items():
            encoded = data if data is None or isinstance(data, dict) else base64.b64encode(data).decode("ascii")
            entries.append([rel, encoded])
        payload = {"kind": "directory", "entries": entries}
        if "modes" in snapshot:
            payload["modes"] = snapshot["modes"]
    else:
        raise ValueError("unsupported snapshot kind")
    envelope = {
        "version": _SNAPSHOT_FORMAT_VERSION,
        "path": str(path),
        "snapshot": payload,
    }
    return json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _decode_snapshot(data: bytes, expected_path: Path) -> Snapshot:
    envelope = json.loads(data.decode("utf-8"))
    if not isinstance(envelope, dict) or envelope.get("version") != _SNAPSHOT_FORMAT_VERSION:
        raise ValueError("unsupported snapshot format")
    if envelope.get("path") != str(expected_path):
        raise ValueError("snapshot path does not match its store")
    payload = envelope.get("snapshot")
    if not isinstance(payload, dict):
        raise ValueError("snapshot payload is not an object")
    kind = payload.get("kind")
    if kind == "missing":
        return None
    if kind == "file":
        encoded = payload.get("data")
        if not isinstance(encoded, str):
            raise ValueError("file snapshot has no data")
        decoded = base64.b64decode(encoded, validate=True)
        if "mode" in payload:
            return {"kind": "file", "data": decoded, "mode": _snapshot_mode(payload["mode"]),
                    "read_coverage": payload.get("read_coverage")}
        return decoded
    if kind == "symlink":
        target = payload.get("target")
        if not isinstance(target, str):
            raise ValueError("symlink snapshot has no target")
        return {"kind": "symlink", "target": target}
    if kind == "directory":
        raw_entries = payload.get("entries")
        if not isinstance(raw_entries, list):
            raise ValueError("directory snapshot has no entries")
        entries: dict[str, Any] = {}
        for raw_entry in raw_entries:
            if not isinstance(raw_entry, list) or len(raw_entry) != 2:
                raise ValueError("invalid directory snapshot entry")
            rel, encoded = raw_entry
            rel_path = Path(rel) if isinstance(rel, str) else Path("..")
            if not isinstance(rel, str) or not rel_path.parts or rel_path.is_absolute() or ".." in rel_path.parts:
                raise ValueError("unsafe directory snapshot path")
            if rel_path.as_posix() != rel.rstrip("/"):
                raise ValueError("noncanonical directory snapshot path")
            if isinstance(encoded, dict):
                if rel.endswith("/") or encoded.get("kind") != "symlink" or not isinstance(encoded.get("target"), str):
                    raise ValueError("invalid directory symlink")
                entries[rel] = encoded
                continue
            if encoded is not None and not isinstance(encoded, str):
                raise ValueError("invalid directory snapshot data")
            entries[rel] = None if encoded is None else base64.b64decode(encoded, validate=True)
        for rel in entries:
            for parent in Path(rel).parents:
                ancestor = entries.get(parent.as_posix())
                if ancestor is not None:
                    raise ValueError("directory entry has a non-directory ancestor")
        result = {"kind": "directory", "entries": entries}
        if "modes" in payload:
            modes = payload["modes"]
            if not isinstance(modes, dict):
                raise ValueError("invalid directory modes")
            for rel, mode in modes.items():
                if rel != "." and (rel not in entries or isinstance(entries[rel], dict)):
                    raise ValueError("mode does not name a file or directory")
                _snapshot_mode(mode)
            result["modes"] = modes
        return result
    if kind == "unavailable":
        reason = payload.get("reason")
        return {"kind": "unavailable", "reason": str(reason or "snapshot was not persisted")}
    raise ValueError(f"unsupported snapshot kind {kind!r}")


def _snapshot_mode(mode: Any) -> int:
    if type(mode) is not int or not 0 <= mode <= 0o7777:
        raise ValueError("invalid snapshot mode")
    return mode


def _snapshot_raw_size(snapshot: Snapshot) -> int:
    if snapshot is None:
        return 0
    if isinstance(snapshot, bytes):
        return len(snapshot)
    if snapshot.get("kind") == "directory":
        return sum(
            len(str(rel).encode("utf-8")) + _snapshot_raw_size(data)
            for rel, data in snapshot.get("entries", {}).items()
        )
    if snapshot.get("kind") == "file":
        return len(snapshot["data"])
    return len(str(snapshot.get("target", "")).encode("utf-8"))


class UndoStore:
    """The undo entries of one ToolContext, by path."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._stacks: dict[Path, list[Snapshot]] = {}
        # The file on disk behind each entry of _stacks; None for an entry
        # kept only in memory.
        self._files: dict[Path, list[Path | None]] = {}
        # Where the entries persist; None keeps them in memory only.
        self.directory: Path | None = None
        # Notices for the call running in each thread, by thread id.
        self._notices: dict[int, list[str]] = {}

    def attach(self, directory: Path | None) -> None:
        """Drop the entries held and take those persisted in ``directory``;
        later entries persist there. None keeps them in memory only."""
        with self._lock:
            self._stacks.clear()
            self._files.clear()
            self.directory = directory
            if directory is not None:
                self._load()

    def count(self, path: Path) -> int:
        """How many entries ``path`` has to undo."""
        with self._lock:
            return len(self._stacks.get(path) or [])

    def _path_dir(self, path: Path) -> Path:
        assert self.directory is not None
        digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()
        return self.directory / digest

    def _notice(self, message: str) -> None:
        ident = threading.get_ident()
        self._notices.setdefault(ident, []).append(message)

    def consume_notices(self) -> list[str]:
        """The notices left for the call running in this thread; it takes them."""
        with self._lock:
            return self._notices.pop(threading.get_ident(), [])

    def _load(self) -> None:
        assert self.directory is not None
        try:
            for temporary in self.directory.glob("*/.*.tmp"):
                try:
                    temporary.unlink()
                except OSError:
                    pass
            path_dirs = sorted(path for path in self.directory.iterdir() if path.is_dir())
        except FileNotFoundError:
            return
        except OSError:
            return
        for path_dir in path_dirs:
            try:
                metadata = json.loads((path_dir / "path.json").read_text(encoding="utf-8"))
                target = Path(metadata["path"])
                if not target.is_absolute():
                    raise ValueError('snapshot target is not absolute')
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                continue
            try:
                entries = sorted(path_dir.glob("*.snapshot"))
            except OSError:
                continue
            for entry in entries:
                try:
                    snapshot = _decode_snapshot(entry.read_bytes(), target)
                except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                    snapshot = {"kind": "corrupt", "reason": f"{type(exc).__name__}: {exc}"}
                self._stacks.setdefault(target, []).append(snapshot)
                self._files.setdefault(target, []).append(entry)
        self._prune()

    def _persist(self, path: Path, snapshot: Snapshot) -> Path | None:
        if self.directory is None:
            return None
        path_dir = self._path_dir(path)
        try:
            metadata = json.dumps(
                {"version": _SNAPSHOT_FORMAT_VERSION, "path": str(path)},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            atomic_write(path_dir / "path.json", metadata)
            too_large = _snapshot_raw_size(snapshot) > _SNAPSHOT_MAX_DISK_BYTES
            encoded = b"" if too_large else _encode_snapshot(path, snapshot)
            if too_large or len(encoded) > _SNAPSHOT_MAX_DISK_BYTES:
                reason = f'snapshot exceeds the {_SNAPSHOT_MAX_DISK_BYTES // (1024 * 1024)} MiB session undo cap'
                encoded = json.dumps(
                    {
                        "version": _SNAPSHOT_FORMAT_VERSION,
                        "path": str(path),
                        "snapshot": {"kind": "unavailable", "reason": reason},
                    },
                    separators=(",", ":"),
                ).encode("utf-8")
                self._notice(f'{reason}; this undo is available only until the process exits')
            stamp = f"{time.time_ns():020d}-{secrets.token_hex(6)}.snapshot"
            entry = path_dir / stamp
            atomic_write(entry, encoded)
            self._prune()
            return entry if entry.exists() else None
        except OSError as exc:
            self._notice(f'could not persist undo snapshot for {path}: {exc}')
            return None

    def _forget_file(self, victim: Path) -> None:
        for files in self._files.values():
            for index, entry in enumerate(files):
                if entry == victim:
                    files[index] = None

    @staticmethod
    def _cleanup_dir(path_dir: Path) -> None:
        try:
            if not any(path_dir.glob("*.snapshot")):
                (path_dir / "path.json").unlink(missing_ok=True)
                path_dir.rmdir()
        except OSError:
            pass

    def _prune(self) -> None:
        assert self.directory is not None
        try:
            entries = list(self.directory.glob("*/*.snapshot"))
            sized = [(entry, entry.stat().st_size) for entry in entries]
        except OSError:
            return
        sized.sort(key=lambda item: item[0].name)
        total = sum(size for _entry, size in sized)
        while sized and (len(sized) > _SNAPSHOT_MAX_ENTRIES or total > _SNAPSHOT_MAX_DISK_BYTES):
            victim, size = sized.pop(0)
            try:
                victim.unlink()
            except OSError:
                break
            total -= size
            self._forget_file(victim)
            self._cleanup_dir(victim.parent)

    def record(self, path: Path, snapshot: Snapshot) -> None:
        """Keep ``snapshot`` as the latest entry of ``path``."""
        with self._lock:
            self._stacks.setdefault(path, []).append(snapshot)
            entry = self._persist(path, snapshot)
            self._files.setdefault(path, []).append(entry)

    def _pop(self, path: Path) -> tuple[bool, Snapshot]:
        with self._lock:
            stack = self._stacks.get(path) or []
            if not stack:
                return False, None
            snapshot = stack.pop()
            files = self._files.get(path) or []
            entry = files.pop() if files else None
            if entry is not None:
                try:
                    entry.unlink(missing_ok=True)
                except OSError:
                    pass
                self._cleanup_dir(entry.parent)
            return True, snapshot

    def discard(self, path: Path) -> None:
        """Drop the latest entry for ``path``."""
        self._pop(path)

    @contextmanager
    def restoring(self, path: Path):
        """Consume the latest entry only on normal exit; retain it on restore errors."""
        with self._lock:
            stack = self._stacks.get(path) or []
            found = bool(stack)
            yield found, stack[-1] if found else None
            if found:
                self._pop(path)

    def capture(self, path: Path, coverage: Coverage) -> None:
        """Keep ``path``'s current state as its latest entry; a file's entry
        also keeps ``coverage``, what the model had seen of it."""
        try:
            if path.is_dir():
                entries: dict[str, Any] = {}
                modes = {".": stat.S_IMODE(path.stat().st_mode)}
                for child in sorted(path.rglob("*")):
                    rel = child.relative_to(path).as_posix()
                    if child.is_symlink():
                        entries[rel] = {"kind": "symlink", "target": os.readlink(child)}
                    elif child.is_dir():
                        entries[rel + "/"] = None
                        modes[rel + "/"] = stat.S_IMODE(child.stat().st_mode)
                    elif child.is_file():
                        entries[rel] = child.read_bytes()
                        modes[rel] = stat.S_IMODE(child.stat().st_mode)
                content: Snapshot = {"kind": "directory", "entries": entries, "modes": modes}
            else:
                content = None
                if path.exists():
                    content = {
                        "kind": "file",
                        "data": path.read_bytes(),
                        "mode": stat.S_IMODE(path.stat().st_mode),
                        "read_coverage": {
                            "hash": coverage.content_hash,
                            "ranges": coverage.ranges,
                            "whole": coverage.whole,
                        },
                    }
        except OSError as exc:
            reason = f'could not capture undo snapshot for {path}: {exc}'
            content = {"kind": "unavailable", "reason": reason}
            self._notice(reason)
        self.record(path, content)
