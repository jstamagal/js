"""The loopback OAuth listener: an idle connection cannot stall it, and only a
hit carrying the expected state ends the wait."""

from __future__ import annotations

import socket
import errno
import threading
import time
import urllib.request

import pytest

from js import oauth_loopback


def test_a_callback_port_conflict_preserves_the_bind_error():
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        port = occupied.getsockname()[1]

        with pytest.raises(oauth_loopback.OAuthCallbackError) as raised:
            oauth_loopback.wait_for_code(
                port=port, path="/cb", expected_state="S", timeout_s=0.1,
                label="probe", success_html="ok", failure_html="bad")

    assert isinstance(raised.value.__cause__, OSError)
    assert raised.value.__cause__.errno == errno.EADDRINUSE


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _start(port: int, timeout_s: float) -> tuple[threading.Thread, dict]:
    result: dict = {}

    def waiter() -> None:
        started = time.monotonic()
        try:
            result["code"] = oauth_loopback.wait_for_code(
                port=port, path="/cb", expected_state="S", timeout_s=timeout_s,
                label="probe", success_html="ok", failure_html="bad")
        except oauth_loopback.OAuthCallbackError as exc:
            result["error"] = str(exc)
        result["took"] = time.monotonic() - started

    thread = threading.Thread(target=waiter, daemon=True)
    thread.start()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:  # wait until it listens
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.02)
    return thread, result


def _get(port: int, query: str) -> int:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/cb?{query}", timeout=3) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def test_idle_connection_does_not_block_the_real_callback():
    port = _free_port()
    thread, result = _start(port, timeout_s=10)
    idle = socket.create_connection(("127.0.0.1", port))  # sends nothing
    try:
        assert _get(port, "state=S&code=CODE123") == 200
        thread.join(5)
    finally:
        idle.close()
    assert result.get("code") == "CODE123"
    assert result["took"] < 4


def test_idle_connection_alone_still_times_out_by_the_deadline():
    port = _free_port()
    thread, result = _start(port, timeout_s=0.6)
    idle = socket.create_connection(("127.0.0.1", port))
    try:
        thread.join(5)
    finally:
        idle.close()
    assert "timed out" in result.get("error", "")
    assert result["took"] < 2


def test_wrong_state_hit_does_not_end_the_wait():
    port = _free_port()
    thread, result = _start(port, timeout_s=10)
    assert _get(port, "state=WRONG&error=access_denied") == 400
    assert _get(port, "state=WRONG&code=EVIL") == 400
    assert _get(port, "state=S&code=REAL") == 200
    thread.join(5)
    assert result.get("code") == "REAL"


def test_error_with_matching_state_aborts():
    port = _free_port()
    thread, result = _start(port, timeout_s=10)
    assert _get(port, "state=S&error=access_denied") == 400
    thread.join(5)
    assert result.get("error") == "probe failed: access_denied"


@pytest.mark.parametrize("hits", [2])
def test_first_matching_hit_wins(hits):
    port = _free_port()
    thread, result = _start(port, timeout_s=10)
    assert _get(port, "state=S&code=FIRST") == 200
    thread.join(5)
    assert result.get("code") == "FIRST"
