"""Subprocess helpers that cap retained stdout/stderr while still draining pipes."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class CappedProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool = False
    stderr_truncated: bool = False


class _StreamCapture:
    """Capped accumulator the reader thread feeds INCREMENTALLY.

    Publishing per-chunk (not at EOF) matters: a grandchild that inherits the
    pipe (`sh -c 'daemon & printf done'`) keeps it open after the child exits,
    so the reader never sees EOF — output already written must still be
    returnable from a snapshot taken after the child is gone.
    """

    def __init__(self, cap: int) -> None:
        self.cap = max(0, int(cap))
        self._kept = bytearray()
        self._truncated = False
        self._lock = threading.Lock()

    def feed(self, chunk: bytes) -> None:
        with self._lock:
            if len(self._kept) < self.cap:
                remaining = self.cap - len(self._kept)
                self._kept.extend(chunk[:remaining])
                self._truncated = self._truncated or len(chunk) > remaining
            else:
                self._truncated = True

    def snapshot(self) -> tuple[bytes, bool]:
        with self._lock:
            return bytes(self._kept), self._truncated


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill the child and, on POSIX, its whole process group (grandchildren
    spawned into the session would otherwise survive a timeout kill and keep
    the box busy)."""
    if sys.platform != "win32":
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        proc.kill()
    proc.wait()


def _run_capped(
    argv: list[str],
    *,
    timeout: int,
    cwd: str | None,
    env: dict[str, str] | None = None,
    cap: int,
) -> CappedProcessResult:
    """Run ``argv`` capturing at most ``cap`` bytes per stream.

    Raises ``subprocess.TimeoutExpired`` like ``subprocess.run``; on timeout the
    whole process tree is killed. After a normal exit, readers get a short
    grace to drain the pipe buffers; a reader still blocked past that (a
    backgrounded grandchild deliberately keeps the pipe open) is stopped and
    the parent's read end is closed. Intentionally-spawned daemons are not
    killed, and whatever was captured so far is returned.
    """
    popen_kwargs: dict = {}
    if sys.platform != "win32":
        popen_kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=env,
        **popen_kwargs,
    )
    captures = {"stdout": _StreamCapture(cap), "stderr": _StreamCapture(cap)}
    stop_readers = threading.Event()

    def _reader(name: str, stream) -> None:
        capture = captures[name]
        try:
            if sys.platform != "win32":
                os.set_blocking(stream.fileno(), False)
            while True:
                if stop_readers.is_set():
                    return
                try:
                    chunk = (
                        stream.read1(65536)
                        if sys.platform == "win32"
                        else os.read(stream.fileno(), 65536)
                    )
                except BlockingIOError:
                    stop_readers.wait(0.01)
                    continue
                if not chunk:
                    return
                capture.feed(chunk)
        except Exception:  # noqa: BLE001 - a dying pipe just ends the capture
            return

    threads = [
        threading.Thread(
            target=_reader,
            args=("stdout", proc.stdout),
            daemon=True,
            name="capped-process-stdout",
        ),
        threading.Thread(
            target=_reader,
            args=("stderr", proc.stderr),
            daemon=True,
            name="capped-process-stderr",
        ),
    ]

    def finish_readers() -> None:
        deadline = time.monotonic() + 2
        for thread in threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in threads):
            stop_readers.set()
            for stream in (proc.stdout, proc.stderr):
                with contextlib.suppress(Exception):
                    os.close(stream.fileno())
            for thread in threads:
                thread.join(timeout=0.5)
        for stream in (proc.stdout, proc.stderr):
            with contextlib.suppress(Exception):
                stream.close()

    for thread in threads:
        thread.start()
    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        finish_readers()
        stdout, _ = captures["stdout"].snapshot()
        stderr, _ = captures["stderr"].snapshot()
        raise subprocess.TimeoutExpired(
            argv, timeout, output=stdout, stderr=stderr
        ) from None
    finish_readers()
    stdout, stdout_truncated = captures["stdout"].snapshot()
    stderr, stderr_truncated = captures["stderr"].snapshot()
    return CappedProcessResult(
        returncode=rc,
        stdout=stdout,
        stderr=stderr,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
    )


def truncation_marker(cap: int, knob: str = "limits.max_bash_output_bytes") -> str:
    return f"[truncated: {knob} ({cap}) reached]"
