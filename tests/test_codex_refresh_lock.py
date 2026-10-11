"""Codex refresh under the store lock (ALL-CLAIMS codex_auth.py:82 HIGH,
codex_provider.py:95 HIGH; bakari DESIGN v3.3 + doc #291/#410, maggie #292).

One POST per expired refresh token system-wide: the first holder of the
store lock refreshes and saves, every other caller (task, thread or
process) re-reads the rotated token and adopts it with no POST of its own.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

from js import codex_auth, logins, paths

PROVIDER = "openai-codex"


def _jwt(account_id: str = "acct_1") -> str:
    def enc(obj: dict[str, Any]) -> str:
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{enc({'alg': 'none'})}.{enc({'https://api.openai.com/auth': {'chatgpt_account_id': account_id}})}.sig"


class _RotatingTokenServer:
    """A token endpoint that rotates refresh tokens and rejects reuse."""

    def __init__(self, current: str = "R1", delay_s: float = 0.0):
        self.current = current
        self.posted: list[str] = []
        self.delay_s = delay_s
        self.lock = threading.Lock()
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: Any) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                form = dict(p.split("=", 1) for p in self.rfile.read(length).decode().split("&"))
                refresh = form.get("refresh_token", "")
                time.sleep(server.delay_s)
                with server.lock:
                    server.posted.append(refresh)
                    if refresh != server.current:
                        body = json.dumps({"error": "invalid_grant", "error_description": "refresh_token_reused"})
                        self.send_response(400)
                    else:
                        n = int(server.current[1:]) + 1
                        server.current = f"R{n}"
                        body = json.dumps({"access_token": _jwt(), "refresh_token": server.current, "expires_in": 3600})
                        self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body.encode())

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/token"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server(monkeypatch):
    srv = _RotatingTokenServer()
    monkeypatch.setattr(codex_auth, "TOKEN_URL", srv.url)
    yield srv
    srv.close()


@pytest.fixture
def store(tmp_path: Path):
    logins.set_config_dir(tmp_path)
    yield tmp_path
    logins.set_config_dir(paths.login_store_dir())


def _stale_login(refresh: str = "R1") -> logins.Login:
    return logins.Login(
        provider_id=PROVIDER,
        sdk_provider_id=PROVIDER,
        provider_base_url=codex_auth.CODEX_BASE_URL,
        provider_api_key=_jwt(),
        provider_headers={"x-custom": "1"},
        codex_refresh_token=refresh,
        codex_token_expires=0.0,
        codex_account_id="acct_1",
    )


def test_sync_refresh_posts_once_and_saves(server, store):
    logins.save_login(_stale_login())
    fresh = codex_auth.ensure_fresh_login(_stale_login())
    assert fresh.codex_refresh_token == "R2"
    assert fresh.provider_headers == {"x-custom": "1"}
    assert server.posted == ["R1"]
    assert logins.load_logins()[PROVIDER].codex_refresh_token == "R2"
    # A second caller holding the stale R1 in memory adopts R2 without posting.
    again = codex_auth.ensure_fresh_login(_stale_login())
    assert again.codex_refresh_token == "R2"
    assert server.posted == ["R1"]


def test_async_concurrent_callers_in_one_process_post_once(server, store):
    logins.save_login(_stale_login())

    async def scenario():
        results = await asyncio.gather(*(codex_auth.ensure_fresh_login_async(_stale_login()) for _ in range(6)))
        return [r.codex_refresh_token for r in results]

    assert asyncio.run(scenario()) == ["R2"] * 6
    assert server.posted == ["R1"]


def _child_code(store: Path, token_url: str) -> str:
    return textwrap.dedent(
        f"""
        import sys, time
        sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
        from pathlib import Path
        from js import codex_auth, logins
        logins.set_config_dir(Path({str(store)!r}))
        codex_auth.TOKEN_URL = {token_url!r}
        stale = logins.load_logins()["openai-codex"]  # every child starts from the same stale R1
        # Start together: wait for a shared go time.
        go = float(sys.argv[1])
        time.sleep(max(0.0, go - time.time()))
        fresh = codex_auth.ensure_fresh_login(stale)
        print(fresh.codex_refresh_token)
        """
    )


def test_six_processes_with_the_same_stale_token_post_once(server, store):
    # ruhondo #201 acceptance: 1 POST across processes, all end on R2.
    logins.save_login(_stale_login())
    go = time.time() + 0.6
    procs = [
        subprocess.Popen([sys.executable, "-c", _child_code(store, server.url), str(go)], stdout=subprocess.PIPE, text=True)
        for _ in range(6)
    ]
    outputs = [proc.communicate(timeout=60)[0].strip() for proc in procs]
    assert all(proc.returncode == 0 for proc in procs)
    assert outputs == ["R2"] * 6
    assert server.posted == ["R1"]
    assert logins.load_logins()[PROVIDER].codex_refresh_token == "R2"


def test_rejected_grant_adopts_the_winner_when_the_store_has_a_newer_token(server, store):
    # The file already carries R2 (another process won) but it is expired
    # again; this caller holds burned R1. It must not post R1: it re-reads
    # under the lock, posts the stored token once, and lands on R3.
    server.current = "R2"
    logins.save_login(_stale_login("R2"))
    fresh = codex_auth.ensure_fresh_login(_stale_login("R1"))
    assert fresh.codex_refresh_token == "R3"
    assert server.posted == ["R2"]


def test_rejected_grant_with_nothing_newer_means_relogin_not_a_second_post(server, store):
    server.current = "R9"  # the server rotated behind our back; store still says R1
    logins.save_login(_stale_login("R1"))
    with pytest.raises(RuntimeError, match="js --login openai-codex"):
        codex_auth.ensure_fresh_login(_stale_login("R1"))
    assert server.posted == ["R1"]  # exactly one try, never a re-post of the burned token


def test_unwritable_store_keeps_the_rotated_token_in_memory(server, store, capsys):
    # kivu #147 / mikeno #515: save fails -> turn still gets R2, warning says
    # so, and the NEXT read in this process uses R2 instead of re-posting R1.
    logins.save_login(_stale_login())
    os.chmod(store, 0o555)
    try:
        fresh = codex_auth.ensure_fresh_login(_stale_login())
        assert fresh.codex_refresh_token == "R2"
        assert "not saved" in capsys.readouterr().err
        assert logins.load_logins()[PROVIDER].codex_refresh_token == "R1"  # disk unchanged
        again = codex_auth.ensure_fresh_login(_stale_login())
        assert again.codex_refresh_token == "R2"
        assert server.posted == ["R1"]
    finally:
        os.chmod(store, 0o755)


def test_cancelled_caller_still_lets_the_rotation_reach_the_file(store, monkeypatch):
    # maggie #292 / bakari #406: cancel mid-POST after the server rotated ->
    # the save must still land under the held lock, or the next holder
    # posts a burned token.
    srv = _RotatingTokenServer(delay_s=0.5)
    monkeypatch.setattr(codex_auth, "TOKEN_URL", srv.url)
    try:
        logins.save_login(_stale_login())

        async def scenario():
            task = asyncio.create_task(codex_auth.ensure_fresh_login_async(_stale_login()))
            await asyncio.sleep(0.2)  # the POST is in flight
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())
        assert srv.posted == ["R1"]
        assert logins.load_logins()[PROVIDER].codex_refresh_token == "R2"
    finally:
        srv.close()


def test_lock_timeout_adopts_a_fresh_file_or_refuses_to_post_unlocked(server, store, monkeypatch):
    monkeypatch.setattr(logins, "_LOCK_TIMEOUT_S", 0.3)
    fresh_on_disk = logins.Login(provider_id=PROVIDER, provider_api_key=_jwt(), codex_refresh_token="R5", codex_token_expires=time.time() + 3600)
    logins.save_login(fresh_on_disk)
    code = textwrap.dedent(
        f"""
        import fcntl, os, time
        fd = os.open({str(logins.lock_path())!r}, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        print("held", flush=True)
        time.sleep(30)
        """
    )
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    assert proc.stdout is not None and proc.stdout.readline().strip() == "held"
    try:
        # Someone holds the lock, but the file is already fresh: adopt it, no POST.
        assert codex_auth.ensure_fresh_login(_stale_login("R1")).codex_refresh_token == "R5"
        # Stale file + lock never freed: refuse, never post unlocked.
        (store / "logins.toml").write_text(
            f'[openai-codex]\nprovider_id = "openai-codex"\nprovider_api_key = "{_jwt()}"\n'
            'codex_refresh_token = "R1"\ncodex_token_expires = 0.0\n',
            encoding="utf-8",
        )
        with pytest.raises(logins.LoginStoreBusyError):
            codex_auth.ensure_fresh_login(_stale_login("R1"))
        assert server.posted == []
    finally:
        proc.kill()
        proc.wait()


def test_httpx_client_timeout_is_bounded_for_the_refresh_post():
    # doc #212: the refresh POST must not hang a holder (and every waiter)
    # forever. The token form helper owns a client with a finite timeout.
    assert codex_auth._TOKEN_TIMEOUT == 15.0
    client = httpx.Client(timeout=codex_auth._TOKEN_TIMEOUT)
    assert client.timeout.read == 15.0
    client.close()


def test_callback_server_ignores_a_hit_with_the_wrong_state(monkeypatch):
    # mikeno #716: a local process (or a page) hitting the callback with a
    # foreign state must not end the login as "state mismatch".
    import socket

    server = codex_auth._CallbackServer(("127.0.0.1", 0), codex_auth._CallbackHandler)
    server.expected_state = "S"
    server.timeout = 1.0
    port = server.server_address[1]

    def hit(query: str) -> None:
        with socket.create_connection(("127.0.0.1", port)) as sock:
            sock.sendall(f"GET {codex_auth.CALLBACK_PATH}?{query} HTTP/1.0\r\nHost: x\r\n\r\n".encode())
            sock.recv(1024)

    try:
        threading.Thread(target=hit, args=("state=WRONG&code=ATTACKER",), daemon=True).start()
        server.handle_request()
        assert server.received_code is None and server.received_state is None
        threading.Thread(target=hit, args=("state=S&code=REAL",), daemon=True).start()
        server.handle_request()
        assert (server.received_state, server.received_code) == ("S", "REAL")
    finally:
        server.server_close()


def test_callback_handler_has_an_idle_timeout():
    assert codex_auth._CallbackHandler.timeout == 5
