"""Subprocess helpers that cap retained stdout/stderr while still draining pipes."""

from __future__ import annotations

import contextlib
import os
import select
import shlex
import signal
import subprocess
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

from . import paths, settings as _settings
from .jail import _IGNORE_SIGINT

# After the process exits, how long its reader threads get to drain the pipes
# before a reader still blocked (a grandchild holding the pipe) is stopped.
READER_GRACE_S = 2.0
# How often a reader with a silent pipe looks for a stop.
READER_STOP_CHECK_S = 0.25


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
        self.newlines = 0
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
            if self._closed:
                handle.close()
        except OSError:
            self._spill_failed = True
            return
        self._spilled = True
        if not self._closed:
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
            self.newlines += chunk.count(b"\n")
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
                try:
                    self._file.close()
                except OSError:
                    self._spill_failed = True
                    self._spilled = False
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
                try:
                    if self._file is not None:
                        self._file.flush()
                    with open(self.spill_path, "rb") as spilled:
                        spilled.seek(start)
                        return spilled.read(end - start)
                except OSError:
                    self._spill_failed = True
                    self._spilled = False
                    if self._file is not None:
                        with contextlib.suppress(OSError):
                            self._file.close()
                        self._file = None
            kept = bytes(self._head[start:min(end, head_len)])
            if end > tail_start:
                kept += bytes(self._tail[max(start, tail_start) - tail_start:end - tail_start])
            return kept

    def excerpt(self, start: int, budget: int, end: int | None = None) -> Excerpt:
        """Bytes ``start`` to ``end`` (default: the end of the stream so far),
        cut to ``budget``: half from the front of the range, half from its end,
        split on UTF-8 character boundaries, and on a line break near the cut.
        ``total`` is ``end``."""
        total = self.total if end is None else min(int(end), self.total)
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
        return Excerpt(head, tail, (start + len(head), total - len(tail)), total,
                       path if self._spilled else None)


def descendants(root_pid: int) -> list[tuple[int, int]]:
    """Pids and process groups below a process, including children in new sessions."""
    children: dict[int, list[tuple[int, int]]] = {}
    try:
        entries = os.scandir("/proc")
    except OSError:
        return []
    with entries:
        for entry in entries:
            if not entry.name.isdecimal():
                continue
            try:
                stat = Path(entry.path, "stat").read_bytes()
                fields = stat[stat.rfind(b")") + 2:].split()
                parent, group = int(fields[1]), int(fields[2])
            except (OSError, ValueError, IndexError):
                continue
            children.setdefault(parent, []).append((int(entry.name), group))
    found = []
    pending = [root_pid]
    while pending:
        for pid, group in children.get(pending.pop(), ()):
            found.append((pid, group))
            pending.append(pid)
    return found


def _session_members(sid: int) -> list[int]:
    """Pids in session ``sid``, read from /proc (empty where there is none)."""
    found = []
    with contextlib.suppress(OSError):
        for entry in os.scandir("/proc"):
            if not entry.name.isdigit():
                continue
            try:
                with open(f"/proc/{entry.name}/stat", "rb") as f:
                    stat = f.read()
            except OSError:
                continue
            # Fields after the command name, which may hold spaces and ')'.
            fields = stat[stat.rfind(b")") + 2:].split()
            if len(fields) > 3 and int(fields[3]) == sid:
                found.append(int(entry.name))
    return found


def _log_kill(proc: subprocess.Popen, members: list[int]) -> None:
    """Append one line to KILL_LOG_FILE (unset = ~/.js/state/kill.log): when,
    which command, the pids hit, and the js calls that led to the kill."""

    path = Path(_settings.live_value("KILL_LOG_FILE") or paths.state_root() / "kill.log").expanduser()
    args = proc.args if isinstance(proc.args, (list, tuple)) else [str(proc.args)]
    args = [str(a) for a in args]

    wrapped = args[len(_IGNORE_SIGINT):] if args[:len(_IGNORE_SIGINT)] == _IGNORE_SIGINT else args
    if (wrapped and Path(wrapped[0]).name == "bwrap"
            and wrapped[1:5] == ["--die-with-parent", "--unshare-ipc", "--unshare-uts", "--unshare-cgroup-try"]
            and "--" in wrapped):
        args = wrapped[wrapped.index("--") + 1:]
    command = shlex.join(args)
    frames = traceback.extract_stack()[:-2][-10:]
    via = " < ".join(f"{Path(f.filename).stem}.{f.name}" for f in reversed(frames))
    line = (f"{time.strftime('%Y-%m-%d %H:%M:%S')} js={os.getpid()} pid={proc.pid} "
            f"session={members} cmd={command[:300]!r} via {via}\n")
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)


def _signal_tree(proc: subprocess.Popen) -> None:
    """SIGKILL the child and everything in its session. The child leads a new
    session, and a grandchild that moves to its own process group (``timeout``
    does, and so does any job-control shell) stays in it; killing the group
    alone left those running and the box busy. Returns without waiting for
    them to die."""
    members = _session_members(proc.pid)
    _log_kill(proc, members)
    for pid in members:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.kill(pid, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        proc.kill()


def _kill_tree(proc: subprocess.Popen) -> None:
    _signal_tree(proc)
    proc.wait()


def _readable(fd: int, timeout: float | None) -> bool:
    """Whether ``fd`` has something to read, or its end, within ``timeout``
    seconds. `poll`, which takes a descriptor of any number."""
    waiter = select.poll()
    waiter.register(fd, select.POLLIN)
    return bool(waiter.poll(None if timeout is None else max(0.0, timeout) * 1000))


def _wait_exit(proc: subprocess.Popen, timeout: float | None) -> int | None:
    """``proc``'s exit status, waiting up to ``timeout`` seconds; None while
    it still runs. Sleeps on the process's pidfd, so the wait ends when the
    process does; where the kernel has none, `Popen.wait` polls for it."""
    if proc.poll() is not None:
        return proc.returncode
    try:
        pidfd = os.pidfd_open(proc.pid)
    except (AttributeError, OSError):
        try:
            return proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None
    try:
        if not _readable(pidfd, timeout):
            return None
    finally:
        os.close(pidfd)
    return proc.wait()


class CappedProcess:
    """A started ``argv`` whose streams are being captured in the background.

    ``wait(timeout)`` blocks up to ``timeout`` seconds and returns the result
    once the process exits, or ``None`` while it is still running — the caller
    decides whether to keep waiting, read a ``stream()``, or ``kill()``.
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

    def stream(self, name: str) -> _StreamCapture:
        """The capture of ``stdout`` or ``stderr``."""
        return self._captures[name]

    def _finish_readers(self) -> None:
        """Give the readers ``READER_GRACE_S`` to drain the pipes, then stop
        the ones still polling (a grandchild holding a pipe keeps them from
        seeing EOF). Each reader closes its own pipe on the way out; this side
        never touches the fds, so no close lands on a number the kernel has
        already handed to another file."""
        deadline = time.monotonic() + READER_GRACE_S
        for thread in self._threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in self._threads):
            self._stop_readers.set()
            for thread in self._threads:
                thread.join(timeout=0.5)
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

    def wait(self, timeout: float | None, *, pipe_idle: float | None = None) -> CappedProcessResult | None:
        """Wait for exit. With pipe_idle, drain active descendants until EOF or
        that many seconds of silence, within the same overall timeout."""
        if self._result is not None:
            return self._result
        deadline = None if timeout is None else time.monotonic() + timeout
        rc = _wait_exit(self.proc, timeout)
        if rc is None:
            return None
        if pipe_idle is not None:
            for thread in self._threads:
                while thread.is_alive():
                    before = sum(stream.total for stream in self._captures.values())
                    wait = pipe_idle if deadline is None else min(pipe_idle, deadline - time.monotonic())
                    thread.join(max(0, wait))
                    if not thread.is_alive():
                        break
                    if deadline is not None and time.monotonic() >= deadline:
                        return None
                    if before == sum(stream.total for stream in self._captures.values()):
                        self._stop_readers.set()
                        break
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
    merge_stderr: bool = False,
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
        stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
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
            fd = stream.fileno()
            os.set_blocking(fd, False)
            while not stop_readers.is_set():
                # Sleep until the pipe has bytes or its end; wake now and
                # then only to see a stop.
                if not _readable(fd, READER_STOP_CHECK_S):
                    continue
                try:
                    chunk = os.read(fd, 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    return
                capture.feed(chunk)
        except Exception:  # noqa: BLE001 - a dying pipe just ends the capture
            return
        finally:
            with contextlib.suppress(Exception):
                stream.close()

    threads = [
        threading.Thread(target=_reader, args=(name, stream), daemon=True,
                         name=f"capped-process-{name}")
        for name, stream in (("stdout", proc.stdout), ("stderr", proc.stderr))
        if stream is not None
    ]

    for thread in threads:
        thread.start()
    return CappedProcess(proc, captures, threads, stop_readers)


def run_capped(
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
    try:
        result = job.wait(timeout)
    except BaseException:
        job.kill()
        raise
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


def truncation_marker(cap: int, knob: str = "SHELL_OUTPUT_MAX") -> str:
    return f'[truncated: {knob} ({cap}) reached]'
