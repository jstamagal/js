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


class CappedProcessTimeout(subprocess.TimeoutExpired):
    """Timeout carrying the killed status and capped-stream metadata."""

    def __init__(
        self,
        cmd: list[str],
        timeout: int,
        *,
        returncode: int,
        output: bytes,
        stderr: bytes,
        stdout_truncated: bool,
        stderr_truncated: bool,
    ) -> None:
        super().__init__(cmd, timeout, output=output, stderr=stderr)
        self.returncode = returncode
        self.stdout_truncated = stdout_truncated
        self.stderr_truncated = stderr_truncated


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


class CappedProcess:
    """A started ``argv`` whose streams are being captured in the background.

    ``wait(timeout)`` blocks up to ``timeout`` seconds and returns the result
    once the process exits, or ``None`` while it is still running — the caller
    decides whether to keep waiting, look at ``snapshot()``, or ``kill()``.
    """

    def __init__(self, proc: subprocess.Popen, captures: dict[str, _StreamCapture],
                 threads: list[threading.Thread], stop_readers: threading.Event) -> None:
        self.proc = proc
        self.started = time.monotonic()
        self._captures = captures
        self._threads = threads
        self._stop_readers = stop_readers
        self._result: CappedProcessResult | None = None
        self._lock = threading.Lock()

    @property
    def pid(self) -> int:
        return self.proc.pid

    def running(self) -> bool:
        return self._result is None and self.proc.poll() is None

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def snapshot(self) -> tuple[bytes, bytes]:
        """Everything captured so far, whether or not the process has exited."""
        return self._captures["stdout"].snapshot()[0], self._captures["stderr"].snapshot()[0]

    def _finish_readers(self) -> None:
        deadline = time.monotonic() + 2
        for thread in self._threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in self._threads):
            self._stop_readers.set()
            for stream in (self.proc.stdout, self.proc.stderr):
                with contextlib.suppress(Exception):
                    os.close(stream.fileno())
            for thread in self._threads:
                thread.join(timeout=0.5)
        for stream in (self.proc.stdout, self.proc.stderr):
            with contextlib.suppress(Exception):
                stream.close()

    def _collect(self, rc: int) -> CappedProcessResult:
        with self._lock:
            if self._result is None:
                self._finish_readers()
                stdout, stdout_truncated = self._captures["stdout"].snapshot()
                stderr, stderr_truncated = self._captures["stderr"].snapshot()
                self._result = CappedProcessResult(
                    returncode=rc, stdout=stdout, stderr=stderr,
                    stdout_truncated=stdout_truncated, stderr_truncated=stderr_truncated,
                )
            return self._result

    def wait(self, timeout: float | None) -> CappedProcessResult | None:
        if self._result is not None:
            return self._result
        try:
            rc = self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None
        return self._collect(rc)

    def kill(self) -> CappedProcessResult:
        """Kill the whole tree and return what was captured before it died."""
        if self._result is not None:
            return self._result
        _kill_tree(self.proc)
        return self._collect(self.proc.returncode)


def start_capped(
    argv: list[str],
    *,
    cwd: str | None,
    env: dict[str, str] | None = None,
    cap: int,
) -> CappedProcess:
    """Start ``argv`` with both streams captured to at most ``cap`` bytes each.

    Readers run in daemon threads from the first byte, so a snapshot taken
    while the process is still running returns what it has printed so far.
    After exit, readers get a short grace to drain the pipe buffers; a reader
    still blocked past that (a backgrounded grandchild deliberately keeps the
    pipe open) is stopped and the parent's read end is closed.
    Intentionally-spawned daemons are not killed.
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

    for thread in threads:
        thread.start()
    return CappedProcess(proc, captures, threads, stop_readers)


def _run_capped(
    argv: list[str],
    *,
    timeout: int,
    cwd: str | None,
    env: dict[str, str] | None = None,
    cap: int,
) -> CappedProcessResult:
    """Run ``argv`` to completion. Raises ``CappedProcessTimeout`` like
    ``subprocess.run``, with the whole process tree killed and whatever was
    captured attached."""
    job = start_capped(argv, cwd=cwd, env=env, cap=cap)
    result = job.wait(timeout)
    if result is None:
        killed = job.kill()
        raise CappedProcessTimeout(
            argv,
            timeout,
            returncode=killed.returncode,
            output=killed.stdout,
            stderr=killed.stderr,
            stdout_truncated=killed.stdout_truncated,
            stderr_truncated=killed.stderr_truncated,
        ) from None
    return result


def truncation_marker(cap: int, knob: str = "limits.max_bash_output_bytes") -> str:
    return f"[truncated: {knob} ({cap}) reached]"
