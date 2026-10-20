"""The session catalog and search index: one SQLite file under ~/.js/cache/.

For each session file it keeps the summary the picker lists (where and how
the session started, its turns, calls and model stamps; see
`js.session_text._Transcript.summary`) and one FTS5 document: every row the
operator and the model wrote, and each tool row's label and call, one row per
line. Tool output is not in it.

`js.session_text.refresh` stores a session here after every write, from the
parse it already holds. `catalog()` walks the sessions folder, compares each
file's size and mtime with what is stored, and parses only the files that
changed since, so a file written by an older js, or while the index was
missing, is caught up on the next open. The file is derived; a missing or
unreadable one is built again from the sessions.
"""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import paths

_SCHEMA = 1
_MARK_OPEN = "\x01"
_MARK_CLOSE = "\x02"
_TOKENIZER = "porter unicode61 remove_diacritics 2"

_lock = threading.RLock()
# This process's open connection: (pid, index path, connection, (st_dev,
# st_ino) of the file it opened).
_current: tuple[int, str, sqlite3.Connection, tuple[int, int] | None] | None = None


def db_path() -> Path:
    return paths.cache_root() / "sessions.sqlite"


def _is_corrupt(exc: sqlite3.Error) -> bool:
    """Whether `exc` says the file is damaged or not a database. Only that
    makes the index worth deleting; a busy or locked database is another
    process at work and passes."""
    code = (getattr(exc, "sqlite_errorcode", None) or 0) & 0xFF
    return code in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB)


def _identity(path: Path) -> tuple[int, int] | None:
    try:
        info = os.stat(path)
    except OSError:
        return None
    return info.st_dev, info.st_ino


def _create(connection: sqlite3.Connection) -> None:
    # Processes that open a missing index together all read user_version 0.
    # The first to take the write lock builds the tables; the others read the
    # version again under the lock and leave them alone.
    connection.execute("BEGIN IMMEDIATE")
    try:
        if connection.execute("PRAGMA user_version").fetchone()[0] != _SCHEMA:
            connection.execute("DROP TABLE IF EXISTS sessions")
            connection.execute("DROP TABLE IF EXISTS docs")
            connection.execute(
                "CREATE TABLE sessions (id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, "
                "size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, info TEXT NOT NULL)")
            connection.execute(
                f"CREATE VIRTUAL TABLE docs USING fts5(text, lines UNINDEXED, tokenize='{_TOKENIZER}')")
            connection.execute(f"PRAGMA user_version = {_SCHEMA}")
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise


def _open(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=5, isolation_level=None, check_same_thread=False)
    try:
        # WAL lets readers through while another process writes. Changing the
        # journal mode takes a lock and fails at once (busy handler skipped)
        # when another opener races it, so it is best-effort: the loser stays
        # in rollback mode, which works. fetchone() steps the cursor so it
        # does not keep holding a shared lock.
        try:
            connection.execute("PRAGMA journal_mode = WAL").fetchone()
        except sqlite3.OperationalError:
            pass
        connection.execute("PRAGMA synchronous = OFF")
        if connection.execute("PRAGMA user_version").fetchone()[0] != _SCHEMA:
            _create(connection)
        connection.execute("SELECT count(*) FROM sessions").fetchone()
    except sqlite3.DatabaseError:
        connection.close()
        raise
    return connection


def _remove(path: Path) -> None:
    for suffix in ("", "-journal", "-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)


class _RebuildLock:
    """Cross-process lock held while the index file is deleted and built
    again. It lives on a sidecar file that is never deleted, so every
    process locks the same inode."""

    def __init__(self, path: Path) -> None:
        self._path = Path(f"{path}.rebuild.lock")
        self._stream = None

    def __enter__(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        stream = open(self._path, "a+b")  # noqa: SIM115 - closed in __exit__
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        except BaseException:
            stream.close()  # __exit__ does not run when __enter__ raises
            raise
        self._stream = stream

    def __exit__(self, *exc_info) -> None:
        self._stream.close()


def _intact(connection: sqlite3.Connection) -> bool:
    """Whether every page of the open index, and the search index's own
    structure, check out. `_open`'s probe reads one small index and passes a
    file whose table pages are damaged. A busy database raises: it says
    nothing either way."""
    try:
        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            return False
        connection.execute("INSERT INTO docs(docs) VALUES ('integrity-check')")
    except sqlite3.DatabaseError as exc:
        if _is_corrupt(exc):
            return False
        raise
    return True


def _recover(path: Path, seen: tuple[int, int] | None = None) -> sqlite3.Connection:
    """A connection to the index after it failed as corrupt. Another process
    may have rebuilt it in the meantime, so under the rebuild lock the file is
    always opened and checked whole again first; only a file that fails that
    check under the lock is deleted and built again. Busy or I/O errors raise
    and delete nothing.

    `seen` (the identity of the file that failed) is not consulted: file
    systems reuse a freed inode number, so the same (dev, ino) can be a
    healthy file another process just built."""
    del seen
    with _RebuildLock(path):
        try:
            connection = _open(path)
        except sqlite3.DatabaseError as exc:
            if not _is_corrupt(exc):
                raise
        else:
            try:
                if _intact(connection):
                    return connection
            except BaseException:
                connection.close()
                raise
            connection.close()
        _remove(path)
        return _open(path)


def _drop_current() -> None:
    global _current
    if _current is not None and _current[0] == os.getpid():
        _current[2].close()
    _current = None


def _connection() -> sqlite3.Connection:
    """This process's connection to the index, created or rebuilt as needed.
    A cached connection is used only while the path still names the file it
    opened; after a rebuild elsewhere it would write into the deleted file."""
    global _current
    path = db_path()
    key = (os.getpid(), str(path))
    if _current is not None and _current[:2] == key:
        opened = _current[3]
        if opened is not None and _identity(path) == opened:
            return _current[2]
    _drop_current()
    seen = _identity(path)
    try:
        connection = _open(path)
    except sqlite3.DatabaseError as exc:
        if not _is_corrupt(exc):
            raise
        # Damaged: it is derived, so it is built again.
        connection = _recover(path, seen)
    _current = (*key, connection, _identity(path))
    return connection


def reset() -> None:
    """Close and delete the index; the next use builds it again."""
    with _lock:
        _drop_current()
        path = db_path()
        with _RebuildLock(path):
            _remove(path)


def _file_key(session_file: Path) -> str:
    return str(Path(session_file).resolve(strict=False))


def store(session_file: Path, summary: dict[str, Any], lines: list[tuple[int, str, float | None, str]],
          *, size: int, mtime_ns: int) -> None:
    """Keep `summary` and the search `lines` of one session, as of `size` bytes."""
    key = _file_key(session_file)
    text = "\n".join(" ".join(line[3].split()) for line in lines)
    meta = json.dumps([[number, who, ts] for number, who, ts, _ in lines], separators=(",", ":"))
    info = json.dumps(summary, separators=(",", ":"), default=str)
    with _lock:
        connection = _connection()
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute("SELECT id FROM sessions WHERE path = ?", (key,)).fetchone()
            if row is None:
                session_id = connection.execute(
                    "INSERT INTO sessions (path, size, mtime_ns, info) VALUES (?, ?, ?, ?)",
                    (key, size, mtime_ns, info)).lastrowid
            else:
                session_id = row[0]
                connection.execute("UPDATE sessions SET size = ?, mtime_ns = ?, info = ? WHERE id = ?",
                                   (size, mtime_ns, info, session_id))
                connection.execute("DELETE FROM docs WHERE rowid = ?", (session_id,))
            connection.execute("INSERT INTO docs (rowid, text, lines) VALUES (?, ?, ?)", (session_id, text, meta))
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise


def forget(session_file: Path) -> None:
    """Drop one session from the index."""
    key = _file_key(session_file)
    with _lock:
        connection = _connection()
        row = connection.execute("SELECT id FROM sessions WHERE path = ?", (key,)).fetchone()
        if row is None:
            return
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM docs WHERE rowid = ?", (row[0],))
        connection.execute("DELETE FROM sessions WHERE id = ?", (row[0],))
        connection.execute("COMMIT")


def _index_file(session_file: Path, info: os.stat_result) -> dict[str, Any] | None:
    from . import session_text

    try:
        transcript = session_text.parse(session_file)
    except OSError:
        return None
    summary = session_text._well_formed(transcript.summary())
    store(session_file, summary, session_text._well_formed(transcript.search_lines()),
          size=info.st_size, mtime_ns=info.st_mtime_ns)
    return summary


def _session_files(root: Path) -> list[Path]:
    try:
        return [path for path in root.rglob("*.jsonl") if not path.name.startswith(".")]
    except OSError:
        return []


def catalog(root: Path | None = None) -> list[dict[str, Any]]:
    """Every session under `root` (the sessions folder by default), each as its
    stored summary plus `path` and `mtime`. A file whose size or mtime differs
    from what is stored is parsed and stored again first; an entry whose file
    is gone is dropped. An index that turns out damaged is built again; a
    locked one raises."""
    try:
        return _catalog_busy_retry(root)
    except sqlite3.DatabaseError as exc:
        if not _is_corrupt(exc):
            raise
        with _lock:
            seen = _current[3] if _current is not None else None
            _drop_current()
            _recover(db_path(), seen).close()
        return _catalog(root)


_BUSY_TRIES = 3


def _is_busy(exc: sqlite3.Error) -> bool:
    code = (getattr(exc, "sqlite_errorcode", None) or 0) & 0xFF
    return code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)


def _catalog_busy_retry(root: Path | None) -> list[dict[str, Any]]:
    """`_catalog`, tried again when another process holds the database past
    the 5s busy timeout; the third busy raises. Nothing is deleted."""
    for attempt in range(_BUSY_TRIES):
        try:
            return _catalog(root)
        except sqlite3.OperationalError as exc:
            if not _is_busy(exc) or attempt == _BUSY_TRIES - 1:
                raise
    raise AssertionError("unreachable")


def _catalog(root: Path | None) -> list[dict[str, Any]]:
    root = Path(paths.sessions_root() if root is None else root)
    prefix = _file_key(root).rstrip("/") + "/"
    with _lock:
        connection = _connection()
        stored = {path: (size, mtime_ns, info) for path, size, mtime_ns, info in connection.execute(
            "SELECT path, size, mtime_ns, info FROM sessions WHERE substr(path, 1, ?) = ?", (len(prefix), prefix))}
        found: list[dict[str, Any]] = []
        for session_file in _session_files(root):
            try:
                info = session_file.stat()
            except OSError:
                continue
            key = _file_key(session_file)
            known = stored.pop(key, None)
            summary = None
            if known is not None and known[0] == info.st_size and known[1] == info.st_mtime_ns:
                try:
                    summary = json.loads(known[2])
                except json.JSONDecodeError:
                    summary = None
            if summary is None:
                summary = _index_file(session_file, info)
            if summary is None:
                continue
            entry = {**summary, "path": key, "mtime": info.st_mtime}
            for link in ("branch_of", "parent"):
                if isinstance(entry.get(link), str):
                    entry[link] = _file_key(Path(entry[link]))
            found.append(entry)
        for gone in stored:
            forget(Path(gone))
    return found


@dataclass(frozen=True)
class Hit:
    """A search hit: its BM25 score (lower ranks higher) and the row it matched best."""

    score: float
    number: int | None = None
    who: str = ""
    ts: float | None = None
    # The matched row's text, and the character spans the query matched in it.
    line: str = ""
    spans: tuple[tuple[int, int], ...] = ()


def _best_line(highlighted: str, meta: list) -> tuple[int, str, tuple[tuple[int, int], ...]]:
    """The index of the line with the most distinct matched terms, its text,
    and the matched spans in it."""
    best = (-1, 0, "", ())
    for index, line in enumerate(highlighted.split("\n")):
        if _MARK_OPEN not in line:
            continue
        plain: list[str] = []
        spans: list[tuple[int, int]] = []
        terms: set[str] = set()
        cursor = 0
        position = 0
        while True:
            start = line.find(_MARK_OPEN, cursor)
            if start < 0:
                plain.append(line[cursor:])
                break
            end = line.find(_MARK_CLOSE, start)
            if end < 0:
                end = len(line)
            plain.append(line[cursor:start])
            position += start - cursor
            word = line[start + 1:end]
            plain.append(word)
            spans.append((position, position + len(word)))
            position += len(word)
            terms.add(word.lower())
            cursor = end + 1
        if len(terms) > best[1]:
            best = (index, len(terms), "".join(plain), tuple(spans))
    return best[0], best[2], best[3]


def search(expression: str, root: Path | None = None) -> dict[str, float]:
    """Session path -> BM25 score for every session under `root` whose text
    matches the FTS5 `expression`. A lower score ranks higher."""
    root = Path(paths.sessions_root() if root is None else root)
    prefix = _file_key(root).rstrip("/") + "/"
    with _lock:
        connection = _connection()
        try:
            rows = connection.execute(
                "SELECT sessions.path, bm25(docs) FROM docs JOIN sessions ON sessions.id = docs.rowid "
                "WHERE docs MATCH ? AND substr(sessions.path, 1, ?) = ?",
                (expression, len(prefix), prefix)).fetchall()
        except sqlite3.OperationalError as exc:
            if exc.sqlite_errorcode != sqlite3.SQLITE_ERROR:
                raise
            return {}  # an expression FTS5 cannot parse matches nothing
    return {path: score for path, score in rows}


def matching_lines(expression: str, scores: dict[str, float]) -> dict[str, Hit]:
    """The best-matching row of each session in `scores`, for the FTS5 `expression`."""
    if not scores:
        return {}
    hits: dict[str, Hit] = {}
    keys = list(scores)
    with _lock:
        connection = _connection()
        for start in range(0, len(keys), 500):
            chunk = keys[start:start + 500]
            marks = ",".join("?" * len(chunk))
            try:
                rows = connection.execute(
                    f"SELECT sessions.path, highlight(docs, 0, ?, ?), docs.lines FROM docs "
                    f"JOIN sessions ON sessions.id = docs.rowid WHERE docs MATCH ? AND sessions.path IN ({marks})",
                    (_MARK_OPEN, _MARK_CLOSE, expression, *chunk)).fetchall()
            except sqlite3.OperationalError as exc:
                if exc.sqlite_errorcode != sqlite3.SQLITE_ERROR:
                    raise
                return hits
            for path, highlighted, meta_text in rows:
                try:
                    meta = json.loads(meta_text)
                except json.JSONDecodeError:
                    meta = []
                index, line, spans = _best_line(highlighted or "", meta)
                number, who, ts = meta[index] if 0 <= index < len(meta) else (None, "", None)
                hits[path] = Hit(scores[path], number, who, ts, line, spans)
    return hits
