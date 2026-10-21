from __future__ import annotations

import os
import signal
import threading
import time

import pytest

from js import capped_process
from js.capped_process import CappedProcessTimeout, _run_capped


def test_excerpt_does_not_advertise_a_spill_that_fails_during_read(tmp_path, monkeypatch):
    capture = capped_process._StreamCapture(8, tmp_path / "spill.log")
    capture.feed(b"abcdefghijklmnop")

    def disk_full():
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(capture._file, "flush", disk_full)
    try:
        excerpt = capture.excerpt(0, 12)
        assert excerpt.head == b"abcd"
        assert excerpt.tail == b"mnop"
        assert excerpt.path is None
    finally:
        capture.close()


def test_run_capped_timeout_attaches_captured_stdout_and_stderr():
    with pytest.raises(CappedProcessTimeout) as raised:
        _run_capped(
            [
                "/bin/sh",
                "-c",
                "printf 'IMPORTANT_PROGRESS_LINE\\n'; "
                "printf 'IMPORTANT_ERROR_LINE\\n' >&2; sleep 30",
            ],
            timeout=0.2,
            cwd=None,
            cap=4096,
        )

    assert raised.value.output == b"IMPORTANT_PROGRESS_LINE\n"
    assert raised.value.stdout == b"IMPORTANT_PROGRESS_LINE\n"
    assert raised.value.stderr == b"IMPORTANT_ERROR_LINE\n"
    assert raised.value.returncode == -signal.SIGKILL
    assert raised.value.stdout_truncated is False
    assert raised.value.stderr_truncated is False


def test_run_capped_timeout_retains_truncation_metadata():
    with pytest.raises(CappedProcessTimeout) as raised:
        _run_capped(
            ["/bin/sh", "-c", "head -c 4096 /dev/zero; sleep 30"],
            timeout=0.2,
            cwd=None,
            cap=64,
        )

    assert raised.value.output == b"\0" * 64
    assert raised.value.stdout_truncated is True
    assert raised.value.stderr_truncated is False


def test_run_capped_stops_readers_when_grandchild_keeps_pipes_open(monkeypatch):
    real_thread = threading.Thread
    created_threads = []

    class ObservedThread(real_thread):
        def __init__(self, *args, target, **kwargs):
            self.finished = threading.Event()

            def observed_target(*target_args, **target_kwargs):
                try:
                    target(*target_args, **target_kwargs)
                finally:
                    self.finished.set()

            super().__init__(*args, target=observed_target, **kwargs)
            created_threads.append(self)

    monkeypatch.setattr(capped_process.threading, "Thread", ObservedThread)
    monkeypatch.setattr(capped_process, "READER_GRACE_S", 0.1)
    result = _run_capped(
        ["/bin/sh", "-c", "sleep 30 & printf '%s\\n' \"$!\""],
        timeout=5,
        cwd=None,
        cap=4096,
    )
    grandchild_pid = int(result.stdout.strip())
    try:
        assert len(created_threads) == 2
        assert all(thread.finished.is_set() for thread in created_threads)
    finally:
        try:
            os.kill(grandchild_pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def test_stopping_readers_never_closes_a_file_opened_under_a_freed_fd(monkeypatch, tmp_path):
    # A grandchild keeps both pipes open, so the readers are stopped after the
    # grace. Each time a descriptor is released, another thread opens a file
    # and gets that number back; that file must still be open afterwards.
    monkeypatch.setattr(capped_process, "READER_GRACE_S", 0.1)
    job = capped_process.start_capped(["/bin/sh", "-c", "sleep 30 &"], cwd=None, cap=4096)
    real_close = os.close
    reopened = []

    def close_then_reopen(fd):
        real_close(fd)
        spare = []
        while len(spare) < 4096:
            new = os.open(tmp_path / f"other-{len(reopened)}-{len(spare)}", os.O_RDWR | os.O_CREAT)
            if new == fd:
                reopened.append((new, os.fstat(new).st_ino))
                break
            spare.append(new)
        for extra in spare:
            real_close(extra)

    monkeypatch.setattr(os, "close", close_then_reopen)
    try:
        assert job.wait(5) is not None
    finally:
        monkeypatch.setattr(os, "close", real_close)
    try:
        for thread in job._threads:
            thread.join(5)
        lost = []
        for fd, inode in reopened:
            try:
                alive = os.fstat(fd).st_ino == inode
            except OSError:
                alive = False
            if alive:
                real_close(fd)
            else:
                lost.append(fd)
        assert lost == []
        assert job.proc.stdout.closed and job.proc.stderr.closed
    finally:
        try:
            os.killpg(job.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_a_reader_busy_past_the_grace_keeps_its_pipe_until_it_exits(monkeypatch):
    monkeypatch.setattr(capped_process, "READER_GRACE_S", 0.05)
    entered = threading.Event()
    release = threading.Event()
    real_feed = capped_process._StreamCapture.feed

    def stuck_feed(self, chunk):
        entered.set()
        release.wait(10)
        real_feed(self, chunk)

    monkeypatch.setattr(capped_process._StreamCapture, "feed", stuck_feed)
    job = capped_process.start_capped(["/bin/sh", "-c", "echo out; sleep 30 &"], cwd=None, cap=4096)
    try:
        assert entered.wait(5)
        assert job.wait(5) is not None
        stdout_reader = job._threads[0]
        assert stdout_reader.is_alive()
        assert not job.proc.stdout.closed
        release.set()
        stdout_reader.join(5)
        assert job.proc.stdout.closed
    finally:
        release.set()
        try:
            os.killpg(job.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_kill_reaches_a_grandchild_that_moved_to_its_own_process_group():
    # `timeout` puts itself in a new process group inside the session.
    job = capped_process.start_capped(
        ["/bin/sh", "-c", "timeout 30 sh -c 'echo $$; exec sleep 30'; true"], cwd=None, cap=4096)
    deadline = time.monotonic() + 5
    while not job.stream("stdout").snapshot()[0].strip() and time.monotonic() < deadline:
        time.sleep(0.01)
    sleeper = int(job.stream("stdout").snapshot()[0])
    assert os.getpgid(sleeper) != job.pid
    job.kill()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            with open(f"/proc/{sleeper}/stat") as f:
                if f.read().rsplit(")", 1)[1].split()[0] == "Z":
                    break
        except FileNotFoundError:
            break
        time.sleep(0.01)
    else:
        os.kill(sleeper, signal.SIGKILL)
        pytest.fail("the grandchild outlived the kill")


def test_a_kill_is_logged_with_its_command(tmp_path, monkeypatch):
    log = tmp_path / "kill.log"
    monkeypatch.setenv("JS_KILL_LOG_FILE", str(log))
    job = capped_process.start_capped(["/bin/sh", "-c", "sleep 30"], cwd=None, cap=4096)
    job.kill()
    assert f"pid={job.pid}" in log.read_text()
    assert "sleep 30" in log.read_text()
