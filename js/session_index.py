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
# This process's open connection: (pid, index path, connection).
_current: tuple[int, str, sqlite3.Connection] | None = None


def db_path() -> Path:
    return paths.cache_root() / "sessions.sqlite"


def _create(connection: sqlite3.Connection) -> None:
    connection.executescript(
        f"""
        DROP TABLE IF EXISTS sessions;
        DROP TABLE IF EXISTS docs;
        CREATE TABLE sessions (
            id INTEGER PRIMARY KEY,
            path TEXT UNIQUE NOT NULL,
            size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            info TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE docs USING fts5(text, lines UNINDEXED, tokenize='{_TOKENIZER}');
        PRAGMA user_version = {_SCHEMA};
        """
    )


def _open(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=5, isolation_level=None, check_same_thread=False)
    try:
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


def _connection() -> sqlite3.Connection:
    """This process's connection to the index, created or rebuilt as needed."""
    global _current
    path = db_path()
    key = (os.getpid(), str(path))
    if _current is not None and _current[:2] == key and path.exists():
        return _current[2]
    if _current is not None and _current[0] == key[0]:
        _current[2].close()
    _current = None
    try:
        connection = _open(path)
    except sqlite3.DatabaseError:
        # Unreadable: it is derived, so it is built again.
        _remove(path)
        connection = _open(path)
    _current = (*key, connection)
    return connection


def reset() -> None:
    """Close and delete the index; the next use builds it again."""
    global _current
    with _lock:
        if _current is not None and _current[0] == os.getpid():
            _current[2].close()
        _current = None
        _remove(db_path())


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
    summary = transcript.summary()
    store(session_file, summary, transcript.search_lines(), size=info.st_size, mtime_ns=info.st_mtime_ns)
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
    is gone is dropped. An index that turns out unreadable is built again."""
    try:
        return _catalog(root)
    except sqlite3.DatabaseError:
        reset()
        return _catalog(root)


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
        except sqlite3.OperationalError:
            return {}
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
            except sqlite3.OperationalError:
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
