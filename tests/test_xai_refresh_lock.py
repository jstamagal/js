"""xAI refresh under the store lock (ALL-CLAIMS xai_auth.py:657 HIGH, :658
MED silent save failure, :659 MED expires_in string, :661 MED endpoint pin).

Same contract as the Codex path: one POST per expired refresh token
system-wide, the rotated token never silently lost, the credential never
posted off x.ai.
"""

from __future__ import annotations

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

import pytest

from js import logins, paths, xai_auth

PROVIDER = "xai-oauth"


class _RotatingTokenServer:
    def __init__(self, current: str = "R1", expires_in: Any = 3600):
        self.current = current
        self.expires_in = expires_in
        self.posted: list[str] = []
        self.lock = threading.Lock()
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: Any) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                form = dict(p.split("=", 1) for p in self.rfile.read(length).decode().split("&"))
                refresh = form.get("refresh_token", "")
                with server.lock:
                    server.posted.append(refresh)
                    if refresh != server.current:
                        body = json.dumps({"error": "invalid_grant", "error_description": "refresh_token_reused"})
                        self.send_response(400)
                    else:
                        server.current = f"R{int(server.current[1:]) + 1}"
                        body = json.dumps({"access_token": f"A-{server.current}", "refresh_token": server.current, "expires_in": server.expires_in})
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
    # The endpoint pin only admits x.ai; tests point the pinned name at the fake.
    monkeypatch.setattr(xai_auth, "_validate_endpoint", lambda raw, field: srv.url)
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
        sdk_provider_id="openai",
        provider_base_url=xai_auth.XAI_BASE_URL,
        provider_api_key="A-old",
        provider_headers={"x-custom": "1"},
        xai_refresh_token=refresh,
        xai_token_expires=0.0,
        xai_token_endpoint="https://auth.x.ai/oauth/token",
    )


def test_refresh_posts_once_then_every_later_resolve_adopts(server, store):
    logins.save_login(_stale_login())
    fresh = xai_auth.ensure_fresh_login(_stale_login())
    assert (fresh.xai_refresh_token, fresh.provider_api_key) == ("R2", "A-R2")
    assert fresh.provider_headers == {"x-custom": "1"}
    # sabyinyo #320: four route resolves holding the stale login -> still one POST.
    for _ in range(4):
        assert xai_auth.ensure_fresh_login(_stale_login()).xai_refresh_token == "R2"
    assert server.posted == ["R1"]


def _child_code(store: Path, token_url: str) -> str:
    return textwrap.dedent(
        f"""
        import sys, time
        sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
        from pathlib import Path
        from js import logins, xai_auth
        logins.set_config_dir(Path({str(store)!r}))
        xai_auth._validate_endpoint = lambda raw, field: {token_url!r}
        stale = logins.load_logins()["xai-oauth"]
        go = float(sys.argv[1])
        time.sleep(max(0.0, go - time.time()))
        print(xai_auth.ensure_fresh_login(stale).xai_refresh_token)
        """
    )


def test_six_processes_with_the_same_stale_token_post_once(server, store):
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


def test_late_stale_save_never_overwrites_a_newer_rotation(server, store):
    # mikeno #731 ACT 4: the production save path with a stale in-memory
    # login must not revert the file to a burned token.
    logins.save_login(_stale_login("R1"))
    winner = xai_auth.ensure_fresh_login(_stale_login("R1"))
    assert winner.xai_refresh_token == "R2"
    stale = logins.Login(provider_id=PROVIDER, provider_api_key="A-stale", xai_refresh_token="R1", xai_token_expires=0.0)
    assert xai_auth.save_refreshed_login(stale, derived_from=_stale_login("R0")) is False
    assert logins.load_logins()[PROVIDER].xai_refresh_token == "R2"


def test_unwritable_store_warns_and_keeps_the_rotated_token(server, store, capsys):
    # mikeno #515 receipt 14: the old code swallowed the save failure and
    # re-posted the burned token on every later resolve.
    logins.save_login(_stale_login())
    os.chmod(store, 0o555)
    try:
        fresh = xai_auth.ensure_fresh_login(_stale_login())
        assert fresh.xai_refresh_token == "R2"
        assert "xAI" in capsys.readouterr().err
        for _ in range(3):
            assert xai_auth.ensure_fresh_login(_stale_login()).xai_refresh_token == "R2"
        assert server.posted == ["R1"]
    finally:
        os.chmod(store, 0o755)


def test_rejected_grant_with_nothing_newer_is_a_relogin_not_a_retry(server, store):
    server.current = "R9"
    logins.save_login(_stale_login("R1"))
    with pytest.raises(RuntimeError, match="js --login xai-oauth"):
        xai_auth.ensure_fresh_login(_stale_login("R1"))
    assert server.posted == ["R1"]


def test_expires_in_string_or_missing_does_not_mean_already_expired(store, monkeypatch):
    srv = _RotatingTokenServer(expires_in="3600")
    monkeypatch.setattr(xai_auth, "_validate_endpoint", lambda raw, field: srv.url)
    try:
        logins.save_login(_stale_login())
        fresh = xai_auth.ensure_fresh_login(_stale_login())
        assert fresh.xai_token_expires > time.time() + 3000
        assert xai_auth.login_needs_refresh(fresh) is False
        assert srv.posted == ["R1"]
    finally:
        srv.close()
    # Missing expires_in: a short default, not 0.0 (which meant "refresh on every read").
    assert xai_auth._expires_at(None) > time.time() + 100
    assert xai_auth._expires_at("junk") > time.time() + 100
    assert xai_auth._expires_at(True) > time.time() + 100


def test_stored_endpoint_off_x_ai_is_refused_before_the_credential_is_posted(store):
    # ALL-CLAIMS xai_auth.py:661: the pin applies to the stored endpoint too.
    bad = logins.Login(provider_id=PROVIDER, provider_api_key="A", xai_refresh_token="R1", xai_token_expires=0.0, xai_token_endpoint="https://evil.example/token")
    logins.save_login(bad)
    with pytest.raises(RuntimeError, match="not on x.ai"):
        xai_auth.ensure_fresh_login(bad)


def test_unparseable_stored_expiry_reads_as_expired():
    login = logins.Login(provider_id=PROVIDER, provider_api_key="A", xai_refresh_token="R1", xai_token_expires=float("nan"))
    assert xai_auth.login_needs_refresh(login) is True
