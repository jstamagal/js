"""A writer must land on the inode the path names, not the one it opened.

ALL-CLAIMS session_store.py HIGH: append opened ("a+b") before flocking, so
a link_file that replaced the file between the open and the lock left the
appender writing into the unlinked inode — the record vanished with a
successful return. Both writers now revalidate (dev, ino) after the lock
and reopen when they differ.
"""

import builtins
import json
import os
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import js.session_store as ss

builtins_open = builtins.open

import js.session_store as ss


def test_append_record_survives_link_file_replace(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text(json.dumps({"role": "user", "content": "old"}) + "\n")

    go = threading.Event()
    done = threading.Event()
    errors = []

    def appender():
        go.wait(10)
        try:
            ss.append(path, {"role": "user", "content": "NEW"})
        except BaseException as exc:  # noqa: BLE001
            errors.append(repr(exc))
        done.set()

    appender_thread = threading.Thread(target=appender)
    appender_thread.start()

    real_replace = ss.os.replace

    def slow_replace(source, target):
        go.set()  # let append() open the old inode and block on flock
        time.sleep(0.3)
        return real_replace(source, target)

    ss.os.replace = slow_replace
    try:
        given = ss.link_file(path)
    finally:
        ss.os.replace = real_replace
    assert done.wait(5)
    appender_thread.join(5)

    text = path.read_text()
    assert given >= 1
    assert not errors, errors
    assert "NEW" in text, "LOST WRITE: append()'s record is gone"


def test_two_link_files_do_not_lose_a_record(tmp_path):
    path = tmp_path / "journal.jsonl"
    path.write_text(json.dumps({"role": "user", "content": "old"}) + "\n")

    late_waiting = threading.Event()
    first_locked = threading.Event()
    errors = []

    real_flock = ss.fcntl.flock
    real_replace = ss.os.replace

    def flock(fd, op):
        name = threading.current_thread().name
        if name == "late-link":
            late_waiting.set()
        result = real_flock(fd, op)
        if name == "first-link":
            first_locked.set()
            assert late_waiting.wait(5), "late linker did not reach lock"
        return result

    def replace(source, target):
        real_replace(source, target)
        if threading.current_thread().name == "first-link":
            ss.append(Path(target), {"role": "user", "content": "NEW"})

    def link(path):
        try:
            ss.link_file(path)
        except BaseException as exc:  # noqa: BLE001
            errors.append(repr(exc))

    with patch_object_flock(flock), patch_object_replace(replace):
        first = threading.Thread(target=link, name="first-link", args=(path,))
        second = threading.Thread(target=link, name="late-link", args=(path,))
        first.start()
        assert first_locked.wait(5)
        second.start()
        first.join(5)
        second.join(5)
        assert not first.is_alive() and not second.is_alive(), "deadlocked"

    assert not errors, errors
    contents = [json.loads(line)["content"] for line in path.read_text().split("\n") if line]
    assert "NEW" in contents


def patch_object_flock(side_effect):
    from unittest.mock import patch

    return patch.object(ss.fcntl, "flock", side_effect=side_effect)


def patch_object_replace(side_effect):
    from unittest.mock import patch

    return patch.object(ss.os, "replace", side_effect=side_effect)


def test_open_locked_closes_the_stream_when_stat_fails(tmp_path, monkeypatch):
    """A failed attempt owns nothing: stream closed AND flock released
    (maggie #905 / titus #923)."""
    import errno as _errno

    path = tmp_path / "s.jsonl"
    path.write_text("x\n")

    real_stat = ss.os.stat
    calls = {"n": 0}

    def flaky_stat(p, *a, **k):
        calls["n"] += 1
        raise PermissionError(_errno.EACCES, "no stat")

    import gc

    monkeypatch.setattr(ss.os, "stat", flaky_stat)
    def call():
        try:
            ss._open_locked(path, "a+b")
        except PermissionError as exc:
            # returning the exception keeps its traceback (and the failed
            # frame's stream local) alive across the check below
            return exc
        return None
    retained = [call()]
    monkeypatch.setattr(ss.os, "stat", real_stat)
    gc.collect()

    # the failed attempt closed its stream: no descriptor left on the file
    file_ino = os.stat(path).st_ino
    left = [fd for fd in os.listdir("/proc/self/fd")
            if _fd_ino(int(fd)) == file_ino]
    assert left == [], left

    # and the lock is free: an immediate open+lock succeeds
    with ss._open_locked(path, "a+b") as stream:
        assert os.fstat(stream.fileno()).st_size > 0
    assert calls["n"] >= 1


def test_open_locked_never_resurrects_a_gone_file(tmp_path, monkeypatch):
    """Only attempt 0 may create; if the path disappears mid-flight the retry
    raises instead of making a fresh empty journal (titus #923.2)."""
    path = tmp_path / "gone.jsonl"
    path.write_text("x\n")

    def stealing_open(file, mode="r", *a, **k):
        if str(file) == str(path) and "a" in mode:
            path.unlink()  # the path vanishes after attempt 0 opened it
        return builtins.open(file, mode, *a, **k)

    monkeypatch.setattr(builtins, "open", stealing_open)
    with pytest.raises(FileNotFoundError):
        ss._open_locked(path, "a+b")
    assert not path.exists()


def test_reopened_append_still_writes_at_the_end(tmp_path):
    """The retry's r+b stream must not write at offset 0 (shinda #1031)."""
    path = tmp_path / "s.jsonl"
    path.write_text(json.dumps({"role": "user", "content": "first"}) + "\n")

    go = threading.Event()
    done = threading.Event()
    errors = []

    def appender():
        go.wait(10)
        try:
            ss.append(path, {"role": "user", "content": "NEW"})
        except BaseException as exc:  # noqa: BLE001
            errors.append(repr(exc))
        done.set()

    thread = threading.Thread(target=appender)
    thread.start()
    real_replace = ss.os.replace

    def slow_replace(source, target):
        go.set()
        time.sleep(0.3)
        return real_replace(source, target)

    ss.os.replace = slow_replace
    try:
        ss.link_file(path)
    finally:
        ss.os.replace = real_replace
    assert done.wait(5)
    thread.join(5)
    assert not errors, errors
    text = path.read_text()
    # the new record is a full, parseable line appended AFTER the old one
    lines = [json.loads(line) for line in text.splitlines() if line.strip()]
    assert lines[-1]["content"] == "NEW"
    assert lines[0]["content"] == "first"

def _fd_ino(fd: int) -> int:
    try:
        return os.fstat(fd).st_ino
    except OSError:
        return -1


def test_chain_reads_in_chunks_with_long_lines(tmp_path):
    """A cold cache over a multi-MB session reads in chunks, never the whole
    file at once (ALL-CLAIMS session_store MED). A line longer than the chunk
    still feeds once, whole."""
    path = tmp_path / "big.jsonl"
    # records with real ids, the shape the chain tracks
    path.write_text(json.dumps({"id": "a" * 8, "parent": None, "role": "user", "content": "x" * (2 << 20)}) + "\n"
                    + json.dumps({"id": "b" * 8, "parent": "a" * 8, "role": "user", "content": "tail"}) + "\n")

    with ss._open_locked(path, "a+b") as stream:
        chain, torn = ss._chain(str(path), stream.fileno())
    assert not torn
    assert chain.leaf == "b" * 8
    assert chain.offset == path.stat().st_size
    assert chain.taken == {"a" * 8, "b" * 8}


def test_chain_lf_split_only(tmp_path):
    """Records split on LF only: \\x0c or \\r inside a JSON string must not
    split a record (doc #1114)."""
    path = tmp_path / "s.jsonl"
    record = {"id": "c" * 8, "parent": None, "role": "user", "content": "page"}
    # raw \x0c/\x0b bytes in the file: what a writer with ensure_ascii=False emits
    body = json.dumps(record, separators=(",", ":"))
    path.write_bytes(body[:-1].encode("utf-8") + b"\x0c\x0b\x0c}\n")
    with ss._open_locked(path, "a+b") as stream:
        chain, torn = ss._chain(str(path), stream.fileno())
    assert not torn
    assert chain.taken == {"c" * 8}
