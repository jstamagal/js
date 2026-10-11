"""A writer must land on the inode the path names, not the one it opened.

ALL-CLAIMS session_store.py HIGH: append opened ("a+b") before flocking, so
a link_file that replaced the file between the open and the lock left the
appender writing into the unlinked inode — the record vanished with a
successful return. Both writers now revalidate (dev, ino) after the lock
and reopen when they differ.
"""

import json
import threading
import time
from pathlib import Path

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
