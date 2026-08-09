from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading

import pytest

from js import capped_process
from js.capped_process import _run_capped


@pytest.mark.skipif(sys.platform == "win32", reason="Unix process-group behavior")
def test_run_capped_timeout_attaches_captured_stdout_and_stderr():
    with pytest.raises(subprocess.TimeoutExpired) as raised:
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


@pytest.mark.skipif(sys.platform == "win32", reason="Unix inherited-pipe behavior")
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
