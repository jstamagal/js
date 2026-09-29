"""Subprocess helpers that cap retained stdout/stderr while still draining pipes."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path


# After the process exits, how long its reader threads get to drain the pipes
# before a reader still blocked (a grandchild holding the pipe) is stopped.
READER_GRACE_S = 2.0


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


def _utf8_end(buf: bytes | bytearray) -> int:
    """Length of ``buf`` without a trailing, incomplete UTF-8 character."""
    for back in range(1, min(4, len(buf)) + 1):
        byte = buf[-back]
        if byte < 0x80:
            return len(buf)
        if byte >= 0xC0:
            need = 2 if byte < 0xE0 else 3 if byte < 0xF0 else 4
            return len(buf) if back >= need else len(buf) - back
    return len(buf)


def _utf8_start(buf: bytes | bytearray) -> int:
    """Offset of the first byte in ``buf`` that is not a continuation byte."""
    for index in range(min(3, len(buf))):
        if not 0x80 <= buf[index] < 0xC0:
            return index
    return min(3, len(buf))


# An excerpt's head ends, and its tail starts, at a line break when one is
# within this many bytes of the cut.
_LINE_SNAP = 256


@dataclass(frozen=True)
class Excerpt:
    """What of one stream's range fits a budget: the head, the tail, and the
    byte range between them that is left out. ``omitted`` is None when the
    whole range fits. ``path`` holds the whole stream, or is None when it was
    never written or could not be."""

    head: bytes
    tail: bytes
    omitted: tuple[int, int] | None
    total: int
    path: Path | None


class _StreamCapture:
    """Capped accumulator the reader thread feeds INCREMENTALLY.

    Publishing per-chunk (not at EOF) matters: a grandchild that inherits the
    pipe (`sh -c 'daemon & printf done'`) keeps it open after the child exits,
    so the reader never sees EOF — output already written must still be
    returnable from a snapshot taken after the child is gone.

    Without ``spill_path`` it keeps the first ``cap`` bytes. With one it keeps
    the first and the last ``cap // 2`` bytes in memory, and once the stream
    passes ``cap`` every byte, from the first, goes to ``spill_path`` as the
    reader receives it. ``ensure_spilled`` writes the file for a stream that is
    still under the cap.
    """

    def __init__(self, cap: int, spill_path: Path | None = None) -> None:
        self.cap = max(0, int(cap))
        self.spill_path = spill_path
        self._head_cap = self.cap if spill_path is None else self.cap // 2
        self._tail_cap = 0 if spill_path is None else self.cap - self._head_cap
        self._head = bytearray()
        self._tail = bytearray()
        self._total = 0
        self._file = None
        self._spilled = False
        self._spill_failed = False
        self._closed = False
        self._lock = threading.Lock()

    def _dropped(self) -> bool:
        return self._total > self._head_cap + self._tail_cap

    def _start_spill(self) -> None:
        if self.spill_path is None or self._spilled or self._spill_failed:
            return
        try:
            self.spill_path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(self.spill_path, "wb")  # noqa: SIM115 - held while the reader feeds it
            handle.write(self._head)
            handle.write(self._tail)
            handle.flush()
        except OSError:
            self._spill_failed = True
            return
        self._spilled = True
        if self._closed:
            handle.close()
        else:
            self._file = handle

    def feed(self, chunk: bytes) -> None:
        with self._lock:
            if (self.spill_path is not None and not self._spilled
                    and self._total + len(chunk) > self._head_cap + self._tail_cap):
                self._start_spill()
            if self._file is not None:
                try:
                    self._file.write(chunk)
                except OSError:
                    self._spill_failed = True
                    self._spilled = False
                    with contextlib.suppress(OSError):
                        self._file.close()
                    self._file = None
            self._total += len(chunk)
            rest = chunk
            room = self._head_cap - len(self._head)
            if room > 0:
                self._head.extend(rest[:room])
                rest = rest[room:]
            if rest and self._tail_cap > 0:
                self._tail.extend(rest)
                if len(self._tail) > self._tail_cap:
                    del self._tail[: len(self._tail) - self._tail_cap]

    def close(self) -> None:
        """Stop writing the spill file; the reader has fed its last chunk."""
        with self._lock:
            self._closed = True
            if self._file is not None:
                with contextlib.suppress(OSError):
                    self._file.close()
                self._file = None

    def ensure_spilled(self) -> Path | None:
        """The path holding the whole stream, writing it now if it is still
        all in memory. None when there is no spill path or it failed."""
        with self._lock:
            if not self._dropped():
                self._start_spill()
            return self.spill_path if self._spilled else None

    @property
    def total(self) -> int:
        with self._lock:
            return self._total

    def snapshot(self) -> tuple[bytes, bool]:
        """The kept bytes and whether any were dropped. Past the cap of a
        spilling capture, the head and tail are joined without their middle."""
        with self._lock:
            return bytes(self._head + self._tail), self._dropped()

    def read(self, start: int, end: int) -> bytes:
        """Bytes ``start``..``end`` of the stream, from memory or the spill
        file. When neither holds the range, only the kept part of it."""
        with self._lock:
            end = min(end, self._total)
            start = max(0, start)
            if start >= end:
                return b""
            head_len = len(self._head)
            tail_start = self._total - len(self._tail)
            if not self._dropped():
                return bytes((self._head + self._tail)[start:end])
            if end <= head_len:
                return bytes(self._head[start:end])
            if start >= tail_start:
                return bytes(self._tail[start - tail_start:end - tail_start])
            if self._spilled and self.spill_path is not None:
                if self._file is not None:
                    self._file.flush()
                try:
                    with open(self.spill_path, "rb") as spilled:
                        spilled.seek(start)
                        return spilled.read(end - start)
                except OSError:
                    pass
            kept = bytes(self._head[start:min(end, head_len)])
            if end > tail_start:
                kept += bytes(self._tail[max(start, tail_start) - tail_start:end - tail_start])
            return kept

    def excerpt(self, start: int, budget: int) -> Excerpt:
        """Bytes ``start`` to the end of the stream, cut to ``budget``: half
        from the front of the range, half from its end, split on UTF-8
        character boundaries, and on a line break near the cut."""
        total = self.total
        start = max(0, min(start, total))
        budget = max(0, int(budget))
        if total - start <= budget:
            return Excerpt(self.read(start, total), b"", None, total, self.spill_path if self._spilled else None)
        path = self.ensure_spilled()
        head_n = budget // 2
        head = self.read(start, start + head_n)
        head = head[:_utf8_end(head)]
        cut = head.rfind(b"\n", max(0, len(head) - _LINE_SNAP))
        head = head[:cut + 1] if cut >= 0 else head
        tail = self.read(total - (budget - head_n), total)
        tail = tail[_utf8_start(tail):]
        cut = tail.find(b"\n", 0, _LINE_SNAP)
        tail = tail[cut + 1:] if 0 <= cut < len(tail) - 1 else tail
        return Excerpt(head, tail, (start + len(head), total - len(tail)), total, path)


def _signal_tree(proc: subprocess.Popen) -> None:
    """SIGKILL the child and its whole process group (grandchildren spawned
    into the session would otherwise survive a timeout kill and keep the box
    busy). Returns without waiting for them to die."""
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        proc.kill()


def _kill_tree(proc: subprocess.Popen) -> None:
    _signal_tree(proc)
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

    def stream(self, name: str) -> _StreamCapture:
        """The capture of ``stdout`` or ``stderr``."""
        return self._captures[name]

    def _finish_readers(self) -> None:
        deadline = time.monotonic() + READER_GRACE_S
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
        for capture in self._captures.values():
            capture.close()

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

    def send_kill(self) -> None:
        """Kill the whole tree without waiting; a blocked ``wait`` then returns."""
        if self._result is None:
            _signal_tree(self.proc)

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
    spill_prefix: Path | None = None,
) -> CappedProcess:
    """Start ``argv`` with both streams captured to at most ``cap`` bytes each.

    With ``spill_prefix``, each stream keeps its head and tail in memory and
    goes whole to ``<spill_prefix>-stdout.log`` / ``-stderr.log`` once it
    passes ``cap`` (see ``_StreamCapture``).

    Readers run in daemon threads from the first byte, so a snapshot taken
    while the process is still running returns what it has printed so far.
    After exit, readers get a short grace to drain the pipe buffers; a reader
    still blocked past that (a backgrounded grandchild deliberately keeps the
    pipe open) is stopped and the parent's read end is closed.
    Intentionally-spawned daemons are not killed.

    The child's stdin is /dev/null. js's own stdin is the terminal the input
    line reads; a child holding it (ssh, an interactive shell) would consume
    the operator's keystrokes for as long as it runs.
    """
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=env,
        start_new_session=True,
    )
    captures = {
        name: _StreamCapture(
            cap, None if spill_prefix is None else spill_prefix.with_name(f"{spill_prefix.name}-{name}.log"))
        for name in ("stdout", "stderr")
    }
    stop_readers = threading.Event()

    def _reader(name: str, stream) -> None:
        capture = captures[name]
        try:
            os.set_blocking(stream.fileno(), False)
            while True:
                if stop_readers.is_set():
                    return
                try:
                    chunk = os.read(stream.fileno(), 65536)
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
