"""A `-p` answer reaches the screen in every output mode, and a failed call
says what failed, retries in sight and ends on the Continue line.

The replayed stream (fixtures/cpa_sonnet_stream.sse) is the body cpa
(CLIProxyAPI) sent for claude-sonnet-5-5: a summarized thinking block, then a
45-item Markdown answer. A terminal or pipe js inherits can be in
non-blocking mode, left so by another program sharing it (nvim and node do,
through libuv); a write bigger than its buffer then stops part way.
"""

from __future__ import annotations

import fcntl
import os
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from js import messages as msgs

STREAM =(Path(__file__).parent / "fixtures" / "cpa_sonnet_stream.sse").read_bytes()
FIRST_ITEM = "Body buffered because streaming was never enabled"
LAST_ITEM = "Idle connection silently dropped by a middlebox"
# What cpa streams when its upstream connection dies after it answered 200.
UPSTREAM_DROPPED = (
    b'event: error\ndata: {"type":"error","error":{"type":"api_error","message":'
    b'"read tcp 10.158.243.173:41446->160.79.104.10:443: read: connection timed out"}}\n\n'
)
_ANSI = re.compile(rb"\x1b\[[0-9;?]*[A-Za-z]")


def _without_answer(stream: bytes) -> bytes:
    """``stream`` with its text block taken out: thinking, then the end."""
    events = stream.split(b"\n\n")
    return b"\n\n".join(event for event in events if b'"index":1' not in event)


class _Replay(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - http.server's name
        self.rfile.read(int(self.headers["Content-Length"]))
        replies = self.server.replies
        body = replies.pop(0) if len(replies) > 1 else replies[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


@pytest.fixture
def serve():
    servers = []

    def start(*replies: bytes) -> str:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Replay)
        server.replies = list(replies)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}"

    yield start
    for server in servers:
        server.shutdown()


def _env(tmp_path: Path, url: str, **settings: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("JS_")}
    env.update(HOME=str(tmp_path), TERM="xterm-256color", JS_PROVIDER="anthropic", JS_BASE_URL=url,
               JS_API_KEY="fixture", JS_MODEL_CATALOG_AUTO_REFRESH="off",
               **{f"JS_{key}": value for key, value in settings.items()})
    return env


def _argv(*flags: str) -> list[str]:
    return [sys.executable, "-m", "js", "-m", "claude-sonnet-5-5", *flags, "-p", "replay"]


def _nonblocking(fd: int) -> None:
    fcntl.fcntl(fd, fcntl.F_SETFL, fcntl.fcntl(fd, fcntl.F_GETFL) | os.O_NONBLOCK)


def _on_terminal(argv: list[str], env: dict[str, str], cwd: Path, *, stall_on: str | None = None,
                 interrupt_on: str | None = None) -> tuple[int, bytes]:
    """Run ``argv`` on a non-blocking terminal that drains at a terminal's
    pace, not at memory speed; its exit code and everything it drew. With
    ``stall_on``, the terminal falls half a second behind once that text is on
    the screen, as one over ssh does; with ``interrupt_on``, ^C then."""
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 110, 0, 0))
    _nonblocking(slave)
    child = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, env=env, cwd=cwd,
                             start_new_session=True)
    os.close(slave)
    drawn = bytearray()
    deadline = time.monotonic() + 120
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(master, 1024)
                except OSError:
                    break
                if not chunk:
                    break
                drawn += chunk
                time.sleep(0.003)
                if stall_on is not None and stall_on in _screen(drawn):
                    time.sleep(0.5)
                    stall_on = None
                if interrupt_on is not None and interrupt_on in _screen(drawn):
                    child.send_signal(signal.SIGINT)
                    interrupt_on = None
            elif child.poll() is not None:
                break
        return child.wait(timeout=30), bytes(drawn)
    finally:
        os.close(master)
        if child.poll() is None:
            child.kill()


def _screen(drawn: bytes) -> str:
    return _ANSI.sub(b"", drawn).decode("utf-8", "replace")


CONTINUE = msgs.CONTINUE_HINT.words(command="").strip()
NO_ANSWER = msgs.NO_ANSWER.words()
INTERRUPTED = msgs.TURN_INTERRUPTED.words()
DROPPED_MESSAGE = "read tcp 10.158.243.173:41446->160.79.104.10:443: read: connection timed out"


def _retry_line(n: int, of: int, seconds: float) -> str:
    return msgs.RETRY_WAIT.words(n=n, of=of, seconds=seconds, failure=f"500 {DROPPED_MESSAGE}")


# The terminal falls behind where the answer is written: plain -p reprints it
# after the call's stats line; -d streams it as it arrives.
@pytest.mark.parametrize(("flags", "stall_on"), [((), "finish=stop"), (("-d",), FIRST_ITEM)],
                         ids=["plain", "debug"])
def test_the_answer_reaches_a_non_blocking_terminal(tmp_path, serve, flags, stall_on):
    code, drawn = _on_terminal(_argv(*flags), _env(tmp_path, serve(STREAM)), tmp_path, stall_on=stall_on)
    screen = _screen(drawn)

    assert code == 0, screen[-2000:]
    assert FIRST_ITEM in screen and LAST_ITEM in screen
    assert screen.index(CONTINUE) > screen.index(LAST_ITEM)


def test_the_answer_reaches_a_non_blocking_pipe(tmp_path, serve):
    read_end, write_end = os.pipe()
    _nonblocking(write_end)
    child = subprocess.Popen(_argv(), stdin=subprocess.DEVNULL, stdout=write_end, stderr=subprocess.PIPE,
                             env=_env(tmp_path, serve(STREAM)), cwd=tmp_path)
    os.close(write_end)
    piped = bytearray()
    with os.fdopen(read_end, "rb", buffering=0) as reader:
        while chunk := reader.read(1024):
            piped += chunk
            time.sleep(0.003)
    stderr = child.stderr.read().decode()
    child.stderr.close()

    assert child.wait(timeout=30) == 0, stderr
    assert FIRST_ITEM in piped.decode() and LAST_ITEM in piped.decode()
    assert CONTINUE in stderr


def test_the_answer_reaches_a_redirected_file(tmp_path, serve):
    out = tmp_path / "answer.txt"
    with out.open("wb") as stdout:
        done = subprocess.run(_argv(), stdin=subprocess.DEVNULL, stdout=stdout, stderr=subprocess.PIPE,
                              env=_env(tmp_path, serve(STREAM)), cwd=tmp_path, timeout=120)

    assert done.returncode == 0, done.stderr.decode()
    assert FIRST_ITEM in out.read_text() and LAST_ITEM in out.read_text()
    assert CONTINUE in done.stderr.decode()


def test_a_turn_that_ends_on_reasoning_says_the_model_returned_no_answer(tmp_path, serve):
    code, drawn = _on_terminal(_argv(), _env(tmp_path, serve(_without_answer(STREAM))), tmp_path)
    screen = _screen(drawn)

    assert code == 1
    assert NO_ANSWER in screen
    assert screen.index(CONTINUE) > screen.index(NO_ANSWER)


def test_an_upstream_error_event_is_retried_in_sight(tmp_path, serve):
    url = serve(UPSTREAM_DROPPED, STREAM)
    code, drawn = _on_terminal(_argv(), _env(tmp_path, url, RETRY_COUNT_MAX="3", RETRY_BACKOFF_START="0",
                                             RETRY_BACKOFF_JITTER="0"), tmp_path)
    screen = _screen(drawn)

    assert code == 0, screen[-2000:]
    assert _retry_line(1, 3, 0) in screen
    assert LAST_ITEM in screen and CONTINUE in screen


def test_a_call_that_keeps_failing_gives_up_in_one_plain_line_and_says_continue(tmp_path, serve):
    url = serve(UPSTREAM_DROPPED)
    code, drawn = _on_terminal(_argv(), _env(tmp_path, url, RETRY_COUNT_MAX="2", RETRY_BACKOFF_START="0",
                                             RETRY_BACKOFF_JITTER="0"), tmp_path)
    screen = _screen(drawn)
    gave_up = msgs.RETRY_GAVE_UP.words(n=2)

    assert code == 1
    assert _retry_line(1, 2, 0) in screen and _retry_line(2, 2, 0) in screen
    assert gave_up in screen
    # The failure itself: its status and the provider's message, not the error's dict.
    last = screen[screen.index(gave_up) + len(gave_up):]
    assert f"500 {DROPPED_MESSAGE}" in last
    assert "{'type'" not in screen
    assert last.index(CONTINUE) > last.index(DROPPED_MESSAGE)


def test_ctrl_c_during_a_retry_wait_ends_the_run_with_the_continue_line(tmp_path, serve):
    url = serve(UPSTREAM_DROPPED)
    started = time.monotonic()
    countdown = msgs.RETRY_COUNTDOWN.words(n=1, of=4, seconds=20)
    code, drawn = _on_terminal(_argv(), _env(tmp_path, url, RETRY_COUNT_MAX="4", RETRY_BACKOFF_START="20",
                                             RETRY_BACKOFF_MAX="20", RETRY_BACKOFF_JITTER="0"), tmp_path,
                               interrupt_on=countdown)
    screen = _screen(drawn)

    assert code == 130, screen[-2000:]
    assert time.monotonic() - started < 25
    assert screen.index(CONTINUE) > screen.index(INTERRUPTED)
