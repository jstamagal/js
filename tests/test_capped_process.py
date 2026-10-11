from __future__ import annotations

import os
import signal
import threading

import pytest

from js import capped_process
from js.capped_process import CappedProcessTimeout, _run_capped


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
