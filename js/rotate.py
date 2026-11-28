"""Move old session records and logs into the archive tree, compressed one
file per original (`js.archive`), and bring them back. `js rotate` is the
command (`js.rotate_cli`).

What one rotation takes, for every session file under the sessions folders
(`js.session_store.folders`, subagent runs included) whose modification
time falls in AGE: the `.jsonl`, the `.txt` beside it, and the lock files
beside it that nothing holds. Then every file under `paths.logs_root()` whose
own modification time falls in AGE, except the logs named like a session
that was left live (the debug log `logs/<agent>/<name>.log` and the screen
log `logs/transcript/<agent>/<name>.log` of a session are named by the
session file's stem: `js.debug_trace.debug_autolog_path`,
`js.transcript._transcript_log_path`). A file already compressed (`.xz`,
`.zst`, ...) is not rotated.

Never rotated: a file modified within `ROTATE_AGE_MIN`, a file any process
has open, a session whose liveness record names a live process (or cannot be
read), and a session whose lock a writer holds.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import (
    archive,
    memory,
    messages as msgs,
    paths,
    session_catalog,
    session_store,
    session_text,
)

_UNITS = {"h": 3600, "d": 86400, "w": 7 * 86400}
_AGE = re.compile(r"\s*([<>])?\s*(\d+(?:\.\d+)?)\s*([hdw])\s*")
# Files a log directory holds already compressed.
_COMPRESSED = frozenset({".xz", ".gz", ".zst", ".bz2", ".lz4", ".zip", ".7z", ".tgz"})
# How much of the selected files a dry run compresses to estimate the result.
_SAMPLE_FILE_BYTES = 8 << 20
_SAMPLE_TOTAL_BYTES = 128 << 20


@dataclass(frozen=True)
class Age:
    """A window of modification times: `older` is "not modified in `seconds`
    or more", else "modified within the last `seconds`"."""

    older: bool
    seconds: float

    def selects(self, mtime: float, now: float, floor: float) -> bool:
        idle = now - mtime
        if idle < floor:
            return False
        return idle >= self.seconds if self.older else idle < self.seconds

    def too_young(self, mtime: float, now: float, floor: float) -> bool:
        """Whether a file this new is past the window's young edge: a session
        this new keeps its logs live."""
        return now - mtime < (self.seconds if self.older else floor)


def parse_age(text: str) -> Age:
    """`30d`, `>30d`: not modified in 30 days or more. `<7d`: modified within
    the last 7 days. Units: h, d, w."""
    match = _AGE.fullmatch(text or "")
    if match is None:
        raise ValueError(msgs.ROTATE_BAD_AGE.said(age=text))
    sign, number, unit = match.groups()
    return Age(older=sign != "<", seconds=float(number) * _UNITS[unit])


def _open_files() -> set[tuple[int, int]]:
    """(st_dev, st_ino) of every file another process this user can see has
    open. This process's own opens are the rotation's."""
    found: set[tuple[int, int]] = set()
    try:
        processes = os.listdir("/proc")
    except OSError:
        return found
    own = str(os.getpid())
    for pid in processes:
        if not pid.isdigit() or pid == own:
            continue
        directory = f"/proc/{pid}/fd"
        try:
            descriptors = os.listdir(directory)
        except OSError:
            continue
        for fd in descriptors:
            try:
                info = os.stat(f"{directory}/{fd}")
            except OSError:
                continue
            found.add((info.st_dev, info.st_ino))
    return found


def _identity(path: Path) -> tuple[int, int] | None:
    try:
        info = path.stat()
    except OSError:
        return None
    return info.st_dev, info.st_ino


# --- what a rotation takes ---------------------------------------------------


@dataclass
class Plan:
    """The files one rotation takes: sessions (each with its `.txt`) and logs,
    and the sessions left live because they are in use."""

    sessions: list[Path] = field(default_factory=list)
    logs: list[Path] = field(default_factory=list)
    in_use: list[Path] = field(default_factory=list)
    kept_stems: set[str] = field(default_factory=set)

    def files(self) -> list[Path]:
        texts = [text for session in self.sessions
                 if (text := session_store.text_path(session)).is_file()]
        return [*self.sessions, *texts, *self.logs]


def _session_files() -> list[Path]:
    found: list[Path] = []
    for folder in session_store.folders():
        with contextlib.suppress(OSError):
            found.extend(path for path in folder.rglob(f"*{session_store.SUFFIX}")
                         if not path.name.startswith(".") and path.is_file() and not path.is_symlink())
    return sorted(found)


def _log_files() -> list[Path]:
    root = paths.logs_root()
    # The operator's own bundles of old logs, each beside its manifest.
    kept = root / "archive"
    found: list[Path] = []
    with contextlib.suppress(OSError):
        found.extend(path for path in root.rglob("*")
                     if not path.name.startswith(".") and path.suffix not in _COMPRESSED
                     and not path.is_relative_to(kept) and path.is_file() and not path.is_symlink())
    return sorted(found)


def _stem(log: Path) -> str:
    return log.name[: -len(log.suffix)] if log.suffix else log.name


def plan(age: Age, *, now: float | None = None, floor: float = 0.0,
         open_files: set[tuple[int, int]] | None = None) -> Plan:
    """What a rotation over `age` takes now. Reads only: no lock is taken
    and nothing is created."""
    now = time.time() if now is None else now
    open_files = _open_files() if open_files is None else open_files
    result = Plan()
    for session in _session_files():
        try:
            mtime = session.stat().st_mtime
        except OSError:
            continue
        selected = age.selects(mtime, now, floor)
        live = selected and (session_catalog.session_in_flight(session) is not False
                             or _identity(session) in open_files)
        if selected and not live:
            result.sessions.append(session)
            continue
        if live:
            result.in_use.append(session)
        if live or age.too_young(mtime, now, floor):
            result.kept_stems.add(session.stem)
    for log in _log_files():
        try:
            mtime = log.stat().st_mtime
        except OSError:
            continue
        if (age.selects(mtime, now, floor) and _stem(log) not in result.kept_stems
                and _identity(log) not in open_files):
            result.logs.append(log)
    return result


def sizes(files: list[Path]) -> list[tuple[Path, int]]:
    sized = []
    for path in files:
        with contextlib.suppress(OSError):
            sized.append((path, path.stat().st_size))
    return sized


def estimate(files: list[Path], fmt: str) -> int:
    """About how many bytes `files` take once compressed in `fmt`: each of
    the largest compressed from its first 8 MiB, up to 128 MiB in all, the
    rest at their combined ratio. The whole of a long log compresses better
    than its start, so this tends to be high."""
    sized = sorted(sizes(files), key=lambda item: -item[1])
    sampled_in = sampled_out = 0
    estimated = 0.0
    rest = 0
    for path, size in sized:
        if sampled_in >= _SAMPLE_TOTAL_BYTES or size == 0:
            rest += size
            continue
        try:
            with open(path, "rb") as stream:
                sample = stream.read(_SAMPLE_FILE_BYTES)
        except OSError:
            rest += size
            continue
        compressor = archive._compressor(fmt)
        packed = len(compressor.compress(sample)) + len(compressor.flush())
        sampled_in += len(sample)
        sampled_out += packed
        estimated += size * packed / max(len(sample), 1)
    if rest:
        estimated += rest * (sampled_out / sampled_in if sampled_in else 1.0)
    return int(estimated)


# --- doing it ----------------------------------------------------------------


@dataclass
class Tally:
    files: int = 0
    before: int = 0
    after: int = 0
    skipped: list[tuple[Path, str]] = field(default_factory=list)
    # The files that could not be read or written, as opposed to left alone.
    failed: int = 0

    def add(self, done: archive.Archived) -> None:
        self.files += 1
        self.before += done.before
        self.after += done.after


def _unused(path: Path):
    """Whether no process has `path` open, asked again just before its removal."""
    identity = _identity(path)
    return lambda: identity not in _open_files()


def _archive_one(path: Path, fmt: str, tally: Tally, *, source=None) -> bool:
    try:
        tally.add(archive.archive_file(path, fmt, source=source, unused=_unused(path)))
    except archive.Changed:
        tally.skipped.append((path, msgs.ROTATE_SKIP_CHANGED.text()))
        return False
    except archive.InUse:
        tally.skipped.append((path, msgs.ROTATE_SKIP_IN_USE.text()))
        return False
    except archive.Conflict as exc:
        tally.skipped.append((path, str(msgs.ROTATE_SKIP_CONFLICT.said(archive=exc))))
        return False
    except OSError as exc:
        tally.skipped.append((path, str(exc)))
        tally.failed += 1
        return False
    return True


def _remove_unheld(lock: Path) -> None:
    """Remove the lock file `lock` when no process holds it."""
    try:
        stream = open(lock, "rb")  # noqa: SIM115 - closed below
    except OSError:
        return
    with stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return
        if _identity(lock) == (os.fstat(stream.fileno()).st_dev, os.fstat(stream.fileno()).st_ino):
            lock.unlink()


def _rotate_session(session: Path, fmt: str, tally: Tally, open_files: set[tuple[int, int]]) -> bool:
    """Rotate one session file and its `.txt` under the session's own write
    lock; False when it is in use or could not be rotated."""
    try:
        stream = open(session, "rb")  # noqa: SIM115 - closed below
    except OSError as exc:
        tally.skipped.append((session, str(exc)))
        tally.failed += 1
        return False
    with stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            tally.skipped.append((session, msgs.ROTATE_SKIP_IN_USE.text()))
            return False
        if session_catalog.session_in_flight(session) is not False or _identity(session) in open_files:
            tally.skipped.append((session, msgs.ROTATE_SKIP_IN_USE.text()))
            return False
        if not _archive_one(session, fmt, tally, source=stream):
            return False
        text = session_store.text_path(session)
        if text.is_file():
            _archive_one(text, fmt, tally)
    state, liveness_lock = session_catalog.liveness_paths(session)
    for lock in (liveness_lock, session_text.refresh_lock_path(session), memory.save_lock_path(session)):
        _remove_unheld(lock)
    if session_catalog.session_in_flight(session) is False:
        with contextlib.suppress(OSError):
            state.unlink()
    return True


def rotate(age: Age, fmt: str, *, floor: float = 0.0) -> Tally:
    open_files = _open_files()
    selected = plan(age, floor=floor, open_files=open_files)
    tally = Tally()
    kept = set(selected.kept_stems)
    for session in selected.sessions:
        if not _rotate_session(session, fmt, tally, open_files):
            kept.add(session.stem)
    for log in selected.logs:
        if _stem(log) in kept:
            continue
        if _identity(log) in _open_files():
            tally.skipped.append((log, msgs.ROTATE_SKIP_IN_USE.text()))
            continue
        _archive_one(log, fmt, tally)
    return tally


# --- un-rotating ---------------------------------------------------------------


def archived_files() -> list[Path]:
    """The live path of every file in the archive tree."""
    root = paths.archive_root()
    found: list[Path] = []
    with contextlib.suppress(OSError):
        for archived in root.rglob("*"):
            if archived.name.startswith(".") or not archived.is_file():
                continue
            live = archive.live_path(archived)
            if live is not None:
                found.append(live)
    return sorted(set(found))


def session_archived_files(session: Path) -> list[Path]:
    """The archived files of one session: its record, its `.txt`, its
    subagent runs (and theirs), and the logs named by any of their stems."""
    own = [session, session_store.text_path(session),
           *archive.files_under(session_store.subagent_folder(session), "*")]
    stems = {path.stem for path in own if path.suffix == session_store.SUFFIX}
    logs = [path for path in archived_files()
            if path.is_relative_to(paths.logs_root()) and _stem(path) in stems]
    return [path for path in [*own, *logs] if archive.is_archived(path)]


def restore_all(files: list[Path]) -> tuple[int, int, list[tuple[Path, str]]]:
    restored = size = 0
    failed: list[tuple[Path, str]] = []
    for live in files:
        try:
            if archive.restore(live):
                restored += 1
                size += live.stat().st_size
        except OSError as exc:
            failed.append((live, str(exc)))
    return restored, size, failed
