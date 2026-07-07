"""Subprocess helpers that cap retained stdout/stderr while still draining pipes."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import threading
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
    backgrounded grandchild deliberately keeps the pipe open) is left parked —
    intentionally-spawned daemons are not killed — and whatever was captured
    so far is returned.
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

    def _reader(name: str, stream) -> None:
        capture = captures[name]
        try:
            while True:
                # read1: return whatever is available instead of blocking for a
                # full 64 KiB — chunks must publish as they arrive, not at EOF.
                chunk = stream.read1(65536)
                if not chunk:
                    return
                capture.feed(chunk)
        except Exception:  # noqa: BLE001 - a dying pipe just ends the capture
            return
        finally:
            with contextlib.suppress(Exception):
                stream.close()

    threads = [
        threading.Thread(target=_reader, args=("stdout", proc.stdout), daemon=True),
        threading.Thread(target=_reader, args=("stderr", proc.stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        for thread in threads:
            thread.join(timeout=2)
        raise
    for thread in threads:
        thread.join(timeout=2)
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
