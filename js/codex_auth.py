"""OpenAI Codex OAuth helpers.

Codex uses ChatGPT OAuth credentials, not an OpenAI API key.  The access
JWT is short-lived; the refresh token is durable and must stay in the private
login store, not in the normal js config file.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import selectors
import socket
import sys
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass, replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from .logins import Login


CODEX_PROVIDER_ID = "openai-codex"
CODEX_DEVICE_PROVIDER_ID = "openai-codex-device"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"
CALLBACK_PORT = 1455
CALLBACK_PATH = "/auth/callback"
CALLBACK_REDIRECT_URI = f"http://localhost:{CALLBACK_PORT}{CALLBACK_PATH}"
SCOPE = "openid profile email offline_access"
DEVICE_USERCODE_URL = "https://auth.openai.com/api/accounts/deviceauth/usercode"
DEVICE_TOKEN_URL = "https://auth.openai.com/api/accounts/deviceauth/token"
DEVICE_REDIRECT_URI = "https://auth.openai.com/deviceauth/callback"
DEVICE_AUTH_URL = "https://auth.openai.com/codex/device"
DEFAULT_CODEX_BASE_URL = "https://chatgpt.com/backend-api"
# The Codex model-list endpoint can lag behind the runtime route: gpt-5.5 has
# been verified to work against /codex/responses before the listing endpoint
# advertises it. Both codex_provider.list_models() and picker._model_rows()
# splice this in so it's selectable even when the listing is stale — one
# constant so the two call sites can't drift apart.
CODEX_PHANTOM_MODEL_ID = "gpt-5.5"
_TOKEN_TIMEOUT = 15.0
_DEVICE_POLL_SAFETY_MARGIN = 3.0
_DEVICE_MAX_POLLS = 120
_REFRESH_SKEW_SECONDS = 60.0
_JWT_AUTH_CLAIM = "https://api.openai.com/auth"
_JWT_PROFILE_CLAIM = "https://api.openai.com/profile"


@dataclass(frozen=True)
class CodexToken:
    access: str
    refresh: str
    expires_at: float
    account_id: str | None = None
    email: str | None = None


def normalize_provider_id(provider_id: str) -> str:
    """Collapse login aliases to the durable provider key."""

    return CODEX_PROVIDER_ID if provider_id == CODEX_DEVICE_PROVIDER_ID else provider_id


def is_codex_provider(provider_id: str | None) -> bool:
    return normalize_provider_id(provider_id or "") == CODEX_PROVIDER_ID


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode((data + padding).encode("ascii"))


def decode_jwt_payload(token: str) -> dict[str, Any] | None:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload = json.loads(_b64url_decode(parts[1]).decode("utf-8"))
    except Exception:  # noqa: BLE001 - invalid token payload
        return None
    return payload if isinstance(payload, dict) else None


def token_profile(access_token: str) -> tuple[str | None, str | None]:
    payload = decode_jwt_payload(access_token) or {}
    auth = payload.get(_JWT_AUTH_CLAIM)
    profile = payload.get(_JWT_PROFILE_CLAIM)
    account_id = auth.get("chatgpt_account_id") if isinstance(auth, dict) else None
    email = profile.get("email") if isinstance(profile, dict) else None
    if isinstance(email, str):
        email = email.strip().lower() or None
    else:
        email = None
    return (account_id if isinstance(account_id, str) and account_id else None), email


def _token_from_response(data: dict[str, Any], *, previous: CodexToken | None = None) -> CodexToken:
    access = data.get("access_token")
    refresh = data.get("refresh_token")
    expires_in = data.get("expires_in")
    if not isinstance(access, str) or not access:
        raise RuntimeError("OpenAI Codex token response did not include access_token")
    # Refresh responses may omit a rotated refresh_token; carry the prior one forward.
    if not (isinstance(refresh, str) and refresh):
        refresh = previous.refresh if previous else None
    if not (isinstance(refresh, str) and refresh):
        raise RuntimeError("OpenAI Codex token response did not include refresh_token")
    if not isinstance(expires_in, (int, float)):
        raise RuntimeError("OpenAI Codex token response did not include expires_in")
    account_id, email = token_profile(access)
    if not account_id and previous:
        account_id = previous.account_id
    if not account_id:
        raise RuntimeError("OpenAI Codex access token did not include chatgpt_account_id")
    if not email and previous:
        email = previous.email
    return CodexToken(
        access=access,
        refresh=refresh,
        expires_at=time.time() + float(expires_in),
        account_id=account_id,
        email=email,
    )


def _token_from_login(login: Login) -> CodexToken | None:
    """Snapshot a saved Login as a CodexToken so refreshes can carry forward fields."""
    if not login.codex_refresh_token:
        return None
    return CodexToken(
        access=login.provider_api_key or "",
        refresh=login.codex_refresh_token,
        expires_at=float(login.codex_token_expires) if login.codex_token_expires is not None else 0.0,
        account_id=login.codex_account_id,
        email=login.codex_email,
    )


def _post_token_form(data: dict[str, str], *, client: httpx.Client | None = None, previous: CodexToken | None = None) -> CodexToken:
    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=_TOKEN_TIMEOUT)
    try:
        response = client.post(
            TOKEN_URL,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if response.status_code >= 400:
            detail = _error_detail(response)
            raise RuntimeError(f"OpenAI Codex token request failed: {detail}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("OpenAI Codex token response was not a JSON object")
        return _token_from_response(payload, previous=previous)
    finally:
        if owns_client:
            client.close()


async def _apost_token_form(data: dict[str, str], *, client: httpx.AsyncClient | None = None, previous: CodexToken | None = None) -> CodexToken:
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=_TOKEN_TIMEOUT)
    try:
        response = await client.post(
            TOKEN_URL,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if response.status_code >= 400:
            detail = _error_detail(response)
            raise RuntimeError(f"OpenAI Codex token request failed: {detail}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("OpenAI Codex token response was not a JSON object")
        return _token_from_response(payload, previous=previous)
    finally:
        if owns_client:
            await client.aclose()


def _error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001
        text = response.text.strip()
        return f"HTTP {response.status_code}: {text}" if text else f"HTTP {response.status_code}"
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            message = err.get("message") or err.get("error_description") or err.get("code")
            if message:
                return f"HTTP {response.status_code}: {message}"
        if isinstance(err, str):
            desc = payload.get("error_description")
            return f"HTTP {response.status_code}: {err}{(': ' + desc) if isinstance(desc, str) else ''}"
    return f"HTTP {response.status_code}: {payload!r}"


def exchange_code_for_token(code: str, verifier: str, redirect_uri: str) -> CodexToken:
    return _post_token_form(
        {
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
        }
    )


def refresh_token(refresh: str, *, previous: CodexToken | None = None) -> CodexToken:
    return _post_token_form(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": CLIENT_ID,
        },
        previous=previous,
    )


async def refresh_token_async(refresh: str, *, client: httpx.AsyncClient | None = None, previous: CodexToken | None = None) -> CodexToken:
    return await _apost_token_form(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": CLIENT_ID,
        },
        client=client,
        previous=previous,
    )


def _pkce_pair() -> tuple[str, str]:
    verifier = _b64url(os.urandom(96))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def build_authorize_url(state: str, challenge: str, *, originator: str = "opencode") -> str:
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": CALLBACK_REDIRECT_URI,
        "scope": SCOPE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "originator": originator,
    }
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


class _CallbackHandler(BaseHTTPRequestHandler):
    server: _CallbackServer

    def log_message(self, _format: str, *_args: Any) -> None:  # noqa: A003 - stdlib API
        return

    def do_GET(self) -> None:  # noqa: N802 - stdlib API
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != CALLBACK_PATH:
            self.send_error(404)
            return
        query = urllib.parse.parse_qs(parsed.query)
        self.server.received_state = (query.get("state") or [None])[0]
        self.server.received_code = (query.get("code") or [None])[0]
        self.server.received_error = (query.get("error") or [None])[0]
        ok = self.server.received_code and self.server.received_state == self.server.expected_state
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        if ok:
            body = "<h1>OpenAI Codex login complete</h1><p>You can close this tab and return to js.</p>"
        else:
            body = "<h1>OpenAI Codex login failed</h1><p>Return to js and try again.</p>"
        self.wfile.write(body.encode("utf-8"))


class _CallbackServer(HTTPServer):
    expected_state: str
    received_code: str | None = None
    received_state: str | None = None
    received_error: str | None = None


class _CallbackServerV6(_CallbackServer):
    address_family = socket.AF_INET6


def _bind_callback_servers(handler_cls: type[BaseHTTPRequestHandler]) -> list[_CallbackServer]:
    """Bind the OAuth callback on every loopback family this host supports.

    The registered redirect is ``http://localhost:1455/...``; on a dual-stack
    host ``localhost`` can resolve to ``::1`` first, and a browser that
    doesn't fall back to 127.0.0.1 would sit waiting on a port nothing is
    listening on until the timeout. Binding both loopback addresses on the
    same port removes that race. The redirect string itself must not change —
    it has to match what CLIENT_ID is registered with at OpenAI.
    """
    servers: list[_CallbackServer] = []
    for cls, address in ((_CallbackServer, "127.0.0.1"), (_CallbackServerV6, "::1")):
        try:
            servers.append(cls((address, CALLBACK_PORT), handler_cls))
        except OSError:
            continue  # that family isn't available on this host; the other may still bind
    return servers


def login_browser(*, timeout_s: float = 300.0, originator: str = "opencode") -> Login:
    """Run the fixed-port browser PKCE OAuth flow and return a saved Login."""

    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(24)
    url = build_authorize_url(state, challenge, originator=originator)
    servers = _bind_callback_servers(_CallbackHandler)
    if not servers:
        raise RuntimeError(
            f"OpenAI Codex OAuth could not bind the callback port {CALLBACK_PORT} on 127.0.0.1 or ::1"
        )
    for server in servers:
        server.expected_state = state
        server.timeout = 1.0
    print("Opening browser for OpenAI Codex login...")
    print(f"If it does not open, visit:\n{url}")
    webbrowser.open(url)
    deadline = time.monotonic() + timeout_s
    sel = selectors.DefaultSelector()
    for server in servers:
        sel.register(server, selectors.EVENT_READ, server)
    winner: _CallbackServer | None = None
    try:
        while time.monotonic() < deadline and winner is None:
            for key, _events in sel.select(timeout=1.0):
                srv = key.data
                srv.handle_request()
                if srv.received_code or srv.received_error:
                    winner = srv
                    break
    finally:
        sel.close()
        for server in servers:
            server.server_close()
    if winner is None:
        raise RuntimeError("OpenAI Codex OAuth timed out waiting for browser callback")
    if winner.received_error:
        raise RuntimeError(f"OpenAI Codex OAuth failed: {winner.received_error}")
    if winner.received_state != state:
        raise RuntimeError("OpenAI Codex OAuth state mismatch")
    return login_from_token(exchange_code_for_token(winner.received_code, verifier, CALLBACK_REDIRECT_URI))


def login_device(*, open_browser: bool = True) -> Login:
    """Run the headless Codex device-code OAuth flow and return a saved Login."""


    with httpx.Client(timeout=_TOKEN_TIMEOUT) as client:
        response = client.post(
            DEVICE_USERCODE_URL,
            json={"client_id": CLIENT_ID},
            headers={"Content-Type": "application/json"},
        )
        if response.status_code >= 400:
            raise RuntimeError(f"OpenAI Codex device authorization failed: {_error_detail(response)}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("OpenAI Codex device authorization response was not a JSON object")
        device_auth_id = payload.get("device_auth_id")
        user_code = payload.get("user_code")
        if not isinstance(device_auth_id, str) or not isinstance(user_code, str):
            raise RuntimeError("OpenAI Codex device authorization response missing device_auth_id or user_code")
        raw_interval = payload.get("interval", 5)
        try:
            poll_interval = max(float(raw_interval), 1.0) + _DEVICE_POLL_SAFETY_MARGIN
        except (TypeError, ValueError):
            poll_interval = 5.0 + _DEVICE_POLL_SAFETY_MARGIN

        print("OpenAI Codex login")
        print(f"  URL:  {DEVICE_AUTH_URL}")
        print(f"  Code: {user_code}")
        print("Waiting for authorization...")
        if open_browser:
            webbrowser.open(DEVICE_AUTH_URL)

        for poll in range(_DEVICE_MAX_POLLS):
            time.sleep(min(poll_interval, 5.0) if poll == 0 else poll_interval)
            poll_response = client.post(
                DEVICE_TOKEN_URL,
                json={"device_auth_id": device_auth_id, "user_code": user_code},
                headers={"Content-Type": "application/json"},
            )
            if poll_response.status_code in {403, 404}:
                continue
            if poll_response.status_code >= 400:
                raise RuntimeError(f"OpenAI Codex device token polling failed: {_error_detail(poll_response)}")
            poll_payload = poll_response.json()
            if not isinstance(poll_payload, dict):
                raise RuntimeError("OpenAI Codex device token response was not a JSON object")
            code = poll_payload.get("authorization_code")
            verifier = poll_payload.get("code_verifier")
            if not isinstance(code, str) or not isinstance(verifier, str):
                raise RuntimeError("OpenAI Codex device token response missing authorization_code or code_verifier")
            return login_from_token(exchange_code_for_token(code, verifier, DEVICE_REDIRECT_URI))
    raise RuntimeError("OpenAI Codex device authorization timed out")


def login_from_token(token: CodexToken) -> Login:
    from .logins import Login

    return Login(
        provider_id=CODEX_PROVIDER_ID,
        sdk_provider_id=CODEX_PROVIDER_ID,
        provider_base_url=DEFAULT_CODEX_BASE_URL,
        provider_api_key=token.access,
        codex_refresh_token=token.refresh,
        codex_token_expires=token.expires_at,
        codex_account_id=token.account_id,
        codex_email=token.email,
    )


def login_needs_refresh(login: Login, *, now: float | None = None) -> bool:
    if not is_codex_provider(login.effective_provider_id):
        return False
    if not login.codex_refresh_token:
        return False
    expires = login.codex_token_expires
    if expires is None:
        return True
    return (now if now is not None else time.time()) >= float(expires) - _REFRESH_SKEW_SECONDS


def refreshed_login(login: Login) -> Login:
    if not login.codex_refresh_token:
        raise RuntimeError("OpenAI Codex login has no refresh token; run js --login openai-codex again")
    token = refresh_token(login.codex_refresh_token, previous=_token_from_login(login))
    return replace(login_from_token(token), provider_base_url=login.provider_base_url or DEFAULT_CODEX_BASE_URL)


async def refreshed_login_async(login: Login, *, client: httpx.AsyncClient | None = None) -> Login:
    if not login.codex_refresh_token:
        raise RuntimeError("OpenAI Codex login has no refresh token; run js --login openai-codex again")
    token = await refresh_token_async(login.codex_refresh_token, client=client, previous=_token_from_login(login))
    return replace(login_from_token(token), provider_base_url=login.provider_base_url or DEFAULT_CODEX_BASE_URL)


def save_refreshed_login(refreshed: Login) -> None:
    # A background token refresh must not crash an in-flight turn just
    # because logins.toml is unwritable/corrupt: the caller already has the
    # refreshed token in memory and can keep going, it just won't survive a
    # process restart until the file is fixed.
    from . import logins

    try:
        logins.save_login(refreshed)
    except logins.LoginsCorruptError as exc:
        print(f"*** warning: could not save refreshed OpenAI Codex login: {exc}", file=sys.stderr)


def ensure_fresh_login(login: Login, *, persist: bool = True) -> Login:
    if not login_needs_refresh(login):
        return login
    refreshed = refreshed_login(login)
    if persist:
        save_refreshed_login(refreshed)
    return refreshed


async def ensure_fresh_login_async(login: Login, *, persist: bool = True, client: httpx.AsyncClient | None = None) -> Login:
    if not login_needs_refresh(login):
        return login
    refreshed = await refreshed_login_async(login, client=client)
    if persist:
        save_refreshed_login(refreshed)
    return refreshed


def abort(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(1)
