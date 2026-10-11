"""OpenAI Codex OAuth helpers.

Codex uses ChatGPT OAuth credentials, not an OpenAI API key.  The access
JWT is short-lived; the refresh token is durable and must stay in the private
login store, not in the normal js config file.
"""

from __future__ import annotations

import asyncio
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

from . import messages as msgs

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
CODEX_BASE_URL = "https://chatgpt.com/backend-api"
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
        expires_at=_expiry_or_zero(login.codex_token_expires),
        account_id=login.codex_account_id,
        email=login.codex_email,
    )


def _expiry_or_zero(value: object) -> float:
    """An expiry as a float; anything unreadable counts as already expired."""
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return parsed if parsed == parsed and parsed not in (float("inf"), float("-inf")) else 0.0


def _post_token_form(
    data: dict[str, str], *, client: httpx.Client | None = None, previous: CodexToken | None = None
) -> CodexToken:
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


async def _apost_token_form(
    data: dict[str, str],
    *,
    client: httpx.AsyncClient | None = None,
    previous: CodexToken | None = None,
) -> CodexToken:
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


async def refresh_token_async(
    refresh: str, *, client: httpx.AsyncClient | None = None, previous: CodexToken | None = None
) -> CodexToken:
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
    # An idle connection (browser preconnect, any local client) must not
    # hold the single-threaded server's rfile.readline() forever: the real
    # redirect would then never be read and the deadline never re-checked.
    timeout = 5

    def log_message(self, _format: str, *_args: Any) -> None:  # noqa: A003 - stdlib API
        return

    def do_GET(self) -> None:  # noqa: N802 - stdlib API
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != CALLBACK_PATH:
            self.send_error(404)
            return
        query = urllib.parse.parse_qs(parsed.query)
        state = (query.get("state") or [None])[0]
        code = (query.get("code") or [None])[0]
        error = (query.get("error") or [None])[0]
        # Only a hit carrying this login's state counts. Anything else (a
        # local process, a page pointing the browser here) gets a 400 and
        # changes nothing, so it cannot end the login as "state mismatch".
        ok = bool(code) and state == self.server.expected_state
        if state == self.server.expected_state and (code or error):
            self.server.received_state = state
            self.server.received_code = code
            self.server.received_error = error
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
    msgs.say(msgs.OAUTH_OPENING, service="OpenAI Codex", url=url)
    msgs.say(msgs.OAUTH_PASTE_CALLBACK)
    deadline = time.monotonic() + timeout_s
    sel = selectors.DefaultSelector()
    winner: _CallbackServer | None = None
    pasted = bytearray()
    try:
        for server in servers:
            sel.register(server, selectors.EVENT_READ, server)
        try:
            sel.register(sys.stdin, selectors.EVENT_READ, None)
        except (OSError, ValueError):
            pass  # Captured or redirected stdin may not be selectable.
        webbrowser.open(url)
        while time.monotonic() < deadline and winner is None:
            for key, _events in sel.select(timeout=1.0):
                if key.data is None:
                    chunk = os.read(key.fd, 4096)
                    if not chunk:
                        sel.unregister(key.fileobj)
                        continue
                    pasted.extend(chunk)
                    while b"\n" in pasted:
                        line, _, rest = pasted.partition(b"\n")
                        pasted = bytearray(rest)
                        try:
                            callback = urllib.parse.urlsplit(line.decode("utf-8").strip())
                            query = urllib.parse.parse_qs(callback.query)
                            if (
                                callback.scheme != "http"
                                or callback.netloc != f"localhost:{CALLBACK_PORT}"
                                or callback.path != CALLBACK_PATH
                                or callback.fragment
                                or query.get("state") != [state]
                                or len(query.get("code", [])) != 1
                                or "error" in query
                            ):
                                raise ValueError("Invalid callback")
                        except (ValueError, UnicodeError):
                            msgs.say(msgs.OAUTH_BAD_CALLBACK)
                            continue
                        winner = servers[0]
                        winner.received_state = state
                        winner.received_code = query["code"][0]
                        winner.received_error = None
                        break
                    if winner is not None:
                        break
                    continue
                srv = key.data
                srv.handle_request()
                if srv.received_state == state and (srv.received_code or srv.received_error):
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
    return login_from_token(
        exchange_code_for_token(winner.received_code, verifier, CALLBACK_REDIRECT_URI)
    )


def login_device(*, open_browser: bool = True) -> Login:
    """Run the headless Codex device-code OAuth flow and return a saved Login."""

    with httpx.Client(timeout=_TOKEN_TIMEOUT) as client:
        response = client.post(
            DEVICE_USERCODE_URL,
            json={"client_id": CLIENT_ID},
            headers={"Content-Type": "application/json"},
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"OpenAI Codex device authorization failed: {_error_detail(response)}"
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("OpenAI Codex device authorization response was not a JSON object")
        device_auth_id = payload.get("device_auth_id")
        user_code = payload.get("user_code")
        if not isinstance(device_auth_id, str) or not isinstance(user_code, str):
            raise RuntimeError(
                "OpenAI Codex device authorization response missing device_auth_id or user_code"
            )
        raw_interval = payload.get("interval", 5)
        try:
            poll_interval = max(float(raw_interval), 1.0) + _DEVICE_POLL_SAFETY_MARGIN
        except (TypeError, ValueError):
            poll_interval = 5.0 + _DEVICE_POLL_SAFETY_MARGIN

        msgs.say(msgs.OAUTH_DEVICE, service="OpenAI Codex", url=DEVICE_AUTH_URL, code=user_code)
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
                raise RuntimeError(
                    f"OpenAI Codex device token polling failed: {_error_detail(poll_response)}"
                )
            poll_payload = poll_response.json()
            if not isinstance(poll_payload, dict):
                raise RuntimeError("OpenAI Codex device token response was not a JSON object")
            code = poll_payload.get("authorization_code")
            verifier = poll_payload.get("code_verifier")
            if not isinstance(code, str) or not isinstance(verifier, str):
                raise RuntimeError(
                    "OpenAI Codex device token response missing authorization_code or code_verifier"
                )
            return login_from_token(exchange_code_for_token(code, verifier, DEVICE_REDIRECT_URI))
    raise RuntimeError("OpenAI Codex device authorization timed out")


def login_from_token(token: CodexToken) -> Login:
    from .logins import Login

    return Login(
        provider_id=CODEX_PROVIDER_ID,
        sdk_provider_id=CODEX_PROVIDER_ID,
        provider_base_url=CODEX_BASE_URL,
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
    return (now if now is not None else time.time()) >= _expiry_or_zero(expires) - _REFRESH_SKEW_SECONDS


def apply_refreshed_token(login: Login, token: CodexToken) -> Login:
    """Rotate only the token-derived fields onto the EXISTING login.

    ``login_from_token`` builds a bare Login with just the codex/token fields
    populated — replace()ing its result instead of the real login reset
    provider_headers (and any other field the login carried) to empty on
    every ~hourly refresh. Starting from ``login`` keeps everything else.
    """
    return replace(
        login,
        provider_base_url=login.provider_base_url or CODEX_BASE_URL,
        provider_api_key=token.access,
        codex_refresh_token=token.refresh,
        codex_token_expires=token.expires_at,
        codex_account_id=token.account_id,
        codex_email=token.email,
    )


def refreshed_login(login: Login) -> Login:
    if not login.codex_refresh_token:
        raise RuntimeError(
            "OpenAI Codex login has no refresh token; run js --login openai-codex again"
        )
    token = refresh_token(login.codex_refresh_token, previous=_token_from_login(login))
    return apply_refreshed_token(login, token)


async def refreshed_login_async(login: Login, *, client: httpx.AsyncClient | None = None) -> Login:
    if not login.codex_refresh_token:
        raise RuntimeError(
            "OpenAI Codex login has no refresh token; run js --login openai-codex again"
        )
    token = await refresh_token_async(
        login.codex_refresh_token, client=client, previous=_token_from_login(login)
    )
    return apply_refreshed_token(login, token)


def save_refreshed_login(refreshed: Login, *, derived_from: Login) -> bool:
    """Persist a refreshed login; True when it reached the file.

    Compare-and-save under the store lock: the write happens only while the
    file still carries the refresh token of ``derived_from`` (the login
    whose token was spent to obtain ``refreshed``), or none at all. Any
    other token on disk is another process's newer rotation (or a new
    ``--login``); writing over it would store a burned token and force a
    re-login for everyone (mikeno #731 ACT 4), so the stale save is
    dropped instead. ``derived_from`` is required for that reason (ruhondo
    #966): a rotation writer that cannot say what it rotated from cannot
    be allowed to write.

    A background token refresh must not crash an in-flight turn just
    because logins.toml is unwritable/corrupt: the caller already has the
    refreshed token in memory and can keep going. The rotated token is
    kept process-wide (``logins.note_memory_login``) so the next read in
    this process uses it instead of re-posting the burned one; it will not
    survive a restart until the file is fixed, so the warning says so.
    """
    from . import logins

    try:
        with logins.hold_login_lock():
            stored = logins.load_logins_fresh().get(logins._normalize_provider_id(refreshed.provider_id))
            if stored is None or stored.codex_refresh_token not in (
                derived_from.codex_refresh_token,
                refreshed.codex_refresh_token,
            ):
                # Removed (a --logout), replaced by a key login, or rotated
                # past us by another process: the store's state stands, and
                # nothing of ours is kept, not even in memory (doc #983).
                msgs.warn(msgs.OAUTH_REFRESH_NOT_SAVED, service="OpenAI Codex", error="login changed underneath the refresh")
                return False
            logins.save_login(refreshed)
    except (logins.LoginsCorruptError, logins.LoginStoreBusyError, OSError) as exc:
        logins.note_memory_login(refreshed, derived_from=derived_from)
        msgs.warn(msgs.OAUTH_REFRESH_NOT_SAVED, service="OpenAI Codex", error=exc)
        return False
    return True


def relogin_error(detail: object) -> RuntimeError:
    return RuntimeError(
        f"OpenAI Codex refresh token was rejected ({detail}); run js --login openai-codex again"
    )


def _stored_login(login: Login) -> Login | None:
    """The freshest copy of ``login``: the file re-read now plus the
    in-memory overlay; None when the store holds no such login."""
    from . import logins

    logins.load_logins_fresh()
    return logins.get_login(login.provider_id)


def _adopt_or_refresh_decision(login: Login) -> tuple[Login, bool, bool]:
    """Under the store lock: (login to use, POST still needed, stored).

    ``stored`` False means the store holds no entry for this provider: the
    caller's login is standalone (unsaved, e.g. a login being tested) or was
    removed meanwhile. A standalone refresh may run, but is never persisted:
    writing it would resurrect a ``--logout`` (maggie #955). An unreadable
    store (corrupt TOML) is unknown, not missing: a fresh overlay is used
    with no POST, otherwise the refresh is refused so nothing gets burned
    for a token that could not be saved (ruhondo #1049).
    """
    from . import logins

    current = _stored_login(login)
    if current is None:
        if logins.store_unreadable():
            raise RuntimeError(f"{logins.login_path()} cannot be read; fix it before the OpenAI Codex login can refresh")
        return login, login_needs_refresh(login), False
    if logins.store_unreadable() and login_needs_refresh(current):
        raise RuntimeError(f"{logins.login_path()} cannot be read; fix it before the OpenAI Codex login can refresh")
    return current, login_needs_refresh(current), True


def ensure_fresh_login(login: Login, *, persist: bool = True) -> Login:
    """Return ``login`` with a usable access token, refreshing once if needed.

    The refresh runs under the cross-process store lock, which spans
    re-read -> re-check -> POST -> save: with N processes on one login only
    the first holder posts the refresh token; every waiter then re-reads the
    rotated token from the file and adopts it with no POST of its own. A
    refresh token is single-use once rotated, so a second POST of the same
    one fails and may revoke the whole login (ALL-CLAIMS codex_auth.py:82).
    ``persist=False`` still writes nothing itself; the caller owns the save.
    """
    from . import logins

    if not login_needs_refresh(login):
        return login
    try:
        lock = logins.hold_login_lock()
        lock.__enter__()
    except logins.LoginStoreBusyError:
        # Could not take the lock in time: use the file if someone else
        # refreshed meanwhile; never post an unlocked refresh.
        current = _stored_login(login)
        if current is not None and not login_needs_refresh(current):
            return current
        raise
    try:
        current, needed, stored = _adopt_or_refresh_decision(login)
        if not needed:
            return current
        token = _refresh_or_relogin(current)
        refreshed = apply_refreshed_token(current, token)
        if persist and stored:
            save_refreshed_login(refreshed, derived_from=current)
        return refreshed
    finally:
        lock.__exit__(None, None, None)


def _refresh_or_relogin(current: Login) -> CodexToken:
    assert current.codex_refresh_token
    try:
        return refresh_token(current.codex_refresh_token, previous=_token_from_login(current))
    except RuntimeError as exc:
        if not _is_rejected_grant(exc):
            raise
        # The token we hold was already spent. Under the lock the file is the
        # truth: a different token there is another process's winner, adopt
        # it; the same token means nobody has a newer one -> re-login.
        stored = _stored_login(current)
        if stored is not None and stored.codex_refresh_token and stored.codex_refresh_token != current.codex_refresh_token:
            if not login_needs_refresh(stored):
                return _token_from_login(stored)
            return refresh_token(stored.codex_refresh_token, previous=_token_from_login(stored))
        raise relogin_error(exc) from exc


_REJECTED_GRANT_MARKERS = ("invalid_grant", "refresh_token_reused", "HTTP 400", "HTTP 401")


def _is_rejected_grant(exc: BaseException) -> bool:
    text = str(exc)
    return any(marker in text for marker in _REJECTED_GRANT_MARKERS)


async def ensure_fresh_login_async(
    login: Login, *, persist: bool = True, client: httpx.AsyncClient | None = None
) -> Login:
    """Async twin of ensure_fresh_login: same lock order (per-provider
    asyncio.Lock first, then the store lock polled without blocking the
    loop). The whole hold -> re-read -> POST -> save runs as ONE shielded
    task (the store lock is owned per task, so the saver must be the task
    that took it, maggie #783); a caller cancelled mid-refresh waits,
    bounded, for that task to land the rotation before the cancellation
    propagates, else the next holder would post a burned token.
    """
    from . import logins

    if not login_needs_refresh(login):
        return login
    async with logins.refresh_lock(login.provider_id):
        inner = asyncio.ensure_future(_locked_refresh(login, persist=persist, client=client))
        try:
            return await asyncio.shield(inner)
        except asyncio.CancelledError:
            try:
                await asyncio.wait_for(asyncio.shield(inner), _POST_TIMEOUT_S + logins._LOCK_TIMEOUT_S)
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            raise


_POST_TIMEOUT_S = 20.0


async def _locked_refresh(login: Login, *, persist: bool, client: httpx.AsyncClient | None) -> Login:
    """Hold the store lock across re-read, re-check, POST (bounded) and save."""
    from . import logins

    try:
        async with logins.ahold_login_lock():
            current, needed, stored = _adopt_or_refresh_decision(login)
            if not needed:
                return current
            token = await asyncio.wait_for(_refresh_or_relogin_async(current, client=client), _POST_TIMEOUT_S)
            refreshed = apply_refreshed_token(current, token)
            if persist and stored:
                save_refreshed_login(refreshed, derived_from=current)
            return refreshed
    except logins.LoginStoreBusyError:
        # Could not take the lock in time: use the file if someone else
        # refreshed meanwhile; never post an unlocked refresh.
        current = _stored_login(login)
        if current is not None and not login_needs_refresh(current):
            return current
        raise


async def _refresh_or_relogin_async(current: Login, *, client: httpx.AsyncClient | None) -> CodexToken:
    assert current.codex_refresh_token
    try:
        return await refresh_token_async(
            current.codex_refresh_token, client=client, previous=_token_from_login(current)
        )
    except RuntimeError as exc:
        if not _is_rejected_grant(exc):
            raise
        stored = _stored_login(current)
        if stored is not None and stored.codex_refresh_token and stored.codex_refresh_token != current.codex_refresh_token:
            if not login_needs_refresh(stored):
                return _token_from_login(stored)
            return await refresh_token_async(
                stored.codex_refresh_token, client=client, previous=_token_from_login(stored)
            )
        raise relogin_error(exc) from exc

