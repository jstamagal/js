"""xAI OAuth (subscription login, not an API key).

Grok's console issues API keys, but a Grok subscription can also be spent
directly: an OIDC authorization-code + PKCE login against auth.x.ai returns a
bearer that https://api.x.ai/v1 accepts in place of a key. The endpoints come
from the provider's own discovery document rather than being pinned here, so a
rotation upstream does not need an edit.

The access token is short-lived and lands in ``Login.provider_api_key`` so the
existing provider tuple keeps working unchanged; the refresh token and the
discovered token endpoint ride alongside in ``logins.toml``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

from . import oauth_loopback

if TYPE_CHECKING:
    from .logins import Login

XAI_PROVIDER_ID = "xai-oauth"
DISCOVERY_URL = "https://auth.x.ai/.well-known/openid-configuration"
CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
SCOPE = "openid profile email offline_access grok-cli:access api:access"
DEFAULT_XAI_BASE_URL = "https://api.x.ai/v1"
CALLBACK_PORT = 56121
CALLBACK_PATH = "/callback"
CALLBACK_REDIRECT_URI = f"http://127.0.0.1:{CALLBACK_PORT}{CALLBACK_PATH}"

_TOKEN_TIMEOUT = 15.0
_DISCOVERY_TIMEOUT = 15.0
_REFRESH_SKEW_SECONDS = 300.0
_MAX_RESPONSE_BYTES = 1 << 20


def is_xai_oauth_provider(provider_id: str | None) -> bool:
    return (provider_id or "").strip().lower() == XAI_PROVIDER_ID


@dataclass(frozen=True)
class XaiToken:
    access: str
    refresh: str | None
    expires_at: float
    token_endpoint: str
    email: str | None = None
    subject: str | None = None


@dataclass(frozen=True)
class XaiEndpoints:
    authorize: str
    token: str


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _pkce_pair() -> tuple[str, str]:
    verifier = _b64url(os.urandom(96))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def _validate_endpoint(raw: str | None, field: str) -> str:
    parsed = urllib.parse.urlparse((raw or "").strip())
    if parsed.scheme != "https" or not parsed.hostname:
        raise RuntimeError(f"xAI OAuth discovery returned a non-HTTPS {field}")
    host = parsed.hostname.lower()
    if host != "x.ai" and not host.endswith(".x.ai"):
        # The discovery document decides the endpoints, so pin the only thing
        # that must not move: the credential never leaves x.ai.
        raise RuntimeError(f"xAI OAuth {field} host {host!r} is not on x.ai")
    return parsed.geturl()


def discover(*, client: httpx.Client | None = None) -> XaiEndpoints:
    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=_DISCOVERY_TIMEOUT)
    try:
        response = client.get(DISCOVERY_URL, headers={"Accept": "application/json"})
        body = response.text[:_MAX_RESPONSE_BYTES]
        if response.status_code != 200:
            raise RuntimeError(f"xAI OAuth discovery failed: HTTP {response.status_code}: {body.strip()[:200]}")
        try:
            payload: Any = json.loads(body)
        except ValueError as exc:
            raise RuntimeError(f"xAI OAuth discovery returned malformed JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("xAI OAuth discovery response was not a JSON object")
        return XaiEndpoints(
            authorize=_validate_endpoint(payload.get("authorization_endpoint"), "authorization_endpoint"),
            token=_validate_endpoint(payload.get("token_endpoint"), "token_endpoint"),
        )
    finally:
        if owns_client:
            client.close()


def build_authorize_url(endpoint: str, state: str, nonce: str, challenge: str, *, referrer: str = "js") -> str:
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": CALLBACK_REDIRECT_URI,
        "scope": SCOPE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "nonce": nonce,
        "plan": "generic",
        "referrer": referrer,
    }
    return f"{endpoint}?{urllib.parse.urlencode(params)}"


def _identity_from_id_token(id_token: str | None) -> tuple[str | None, str | None]:
    """Best-effort email/subject out of the id_token; never fatal."""
    parts = (id_token or "").split(".")
    if len(parts) != 3:
        return None, None
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, TypeError):
        return None, None
    if not isinstance(claims, dict):
        return None, None
    email = claims.get("email")
    subject = claims.get("sub")
    return (
        str(email).strip() or None if isinstance(email, str) else None,
        str(subject).strip() or None if isinstance(subject, str) else None,
    )


def _post_token_form(
    endpoint: str,
    data: dict[str, str],
    *,
    client: httpx.Client | None = None,
    previous: XaiToken | None = None,
) -> XaiToken:
    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=_TOKEN_TIMEOUT)
    try:
        response = client.post(
            endpoint,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        )
        body = response.text[:_MAX_RESPONSE_BYTES]
        if response.status_code != 200:
            raise RuntimeError(f"xAI OAuth token request failed: HTTP {response.status_code}: {body.strip()[:200]}")
        try:
            payload: Any = json.loads(body)
        except ValueError as exc:
            raise RuntimeError(f"xAI OAuth token response was malformed JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("xAI OAuth token response was not a JSON object")
        access = str(payload.get("access_token") or "")
        if not access:
            raise RuntimeError("xAI OAuth token response omitted access_token")
        # A refresh grant may legitimately return no new refresh token; keeping
        # the previous one is what makes the session survive past the first hour.
        refresh = str(payload.get("refresh_token") or "") or (previous.refresh if previous else None)
        expires_in = payload.get("expires_in")
        expires_at = time.time() + float(expires_in) if isinstance(expires_in, int | float) else 0.0
        email, subject = _identity_from_id_token(payload.get("id_token"))
        return XaiToken(
            access=access,
            refresh=refresh,
            expires_at=expires_at,
            token_endpoint=endpoint,
            email=email or (previous.email if previous else None),
            subject=subject or (previous.subject if previous else None),
        )
    finally:
        if owns_client:
            client.close()


def exchange_code_for_token(endpoint: str, code: str, verifier: str, *, client: httpx.Client | None = None) -> XaiToken:
    token = _post_token_form(
        endpoint,
        {
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code": code,
            "redirect_uri": CALLBACK_REDIRECT_URI,
            "code_verifier": verifier,
        },
        client=client,
    )
    if not token.refresh:
        raise RuntimeError("xAI OAuth code exchange omitted refresh_token; the login would expire in an hour")
    return token


def refresh_token(refresh: str, endpoint: str, *, previous: XaiToken | None = None, client: httpx.Client | None = None) -> XaiToken:
    return _post_token_form(
        endpoint,
        {"grant_type": "refresh_token", "client_id": CLIENT_ID, "refresh_token": refresh},
        client=client,
        previous=previous,
    )


def login_browser(*, timeout_s: float = 300.0, referrer: str = "js") -> Login:
    """Run the loopback PKCE flow in a browser and return an unsaved Login."""
    endpoints = discover()
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    url = build_authorize_url(endpoints.authorize, state, nonce, challenge, referrer=referrer)
    print("Opening browser for xAI login...")
    print(f"If it does not open, visit:\n{url}")
    webbrowser.open(url)
    code = oauth_loopback.wait_for_code(
        port=CALLBACK_PORT,
        path=CALLBACK_PATH,
        expected_state=state,
        timeout_s=timeout_s,
        label="xAI OAuth",
        success_html="<h1>xAI login complete</h1><p>You can close this tab and return to js.</p>",
        failure_html="<h1>xAI login failed</h1><p>Return to js and try again.</p>",
    )
    return login_from_token(exchange_code_for_token(endpoints.token, code, verifier))


def login_from_token(token: XaiToken) -> Login:
    from .logins import Login

    return Login(
        provider_id=XAI_PROVIDER_ID,
        sdk_provider_id="openai",
        provider_base_url=DEFAULT_XAI_BASE_URL,
        provider_api_key=token.access,
        xai_refresh_token=token.refresh,
        xai_token_expires=token.expires_at,
        xai_token_endpoint=token.token_endpoint,
        xai_email=token.email,
    )


def _token_from_login(login: Login) -> XaiToken:
    return XaiToken(
        access=login.provider_api_key or "",
        refresh=login.xai_refresh_token,
        expires_at=float(login.xai_token_expires) if login.xai_token_expires is not None else 0.0,
        token_endpoint=login.xai_token_endpoint or "",
        email=login.xai_email,
    )


def login_needs_refresh(login: Login, *, now: float | None = None) -> bool:
    if not login.xai_refresh_token:
        return False
    if not login.provider_api_key:
        return True
    expires = login.xai_token_expires
    if expires is None:
        return True
    return (now if now is not None else time.time()) >= float(expires) - _REFRESH_SKEW_SECONDS


def apply_refreshed_token(login: Login, token: XaiToken) -> Login:
    """Fold token-derived fields onto the EXISTING login.

    Rebuilding a bare Login here would drop provider_headers and anything else
    the operator set, on every hourly refresh.
    """
    from dataclasses import replace

    return replace(
        login,
        provider_api_key=token.access,
        xai_refresh_token=token.refresh or login.xai_refresh_token,
        xai_token_expires=token.expires_at,
        xai_token_endpoint=token.token_endpoint or login.xai_token_endpoint,
        xai_email=token.email or login.xai_email,
    )


def save_refreshed_login(refreshed: Login) -> None:
    from . import logins as logins_module

    try:
        logins_module.save_login(refreshed)
    except Exception:  # noqa: BLE001 - a refreshed token in hand beats a failed turn
        return


def ensure_fresh_login(login: Login, *, persist: bool = True) -> Login:
    if not login_needs_refresh(login):
        return login
    endpoint = login.xai_token_endpoint or discover().token
    token = refresh_token(
        login.xai_refresh_token or "",
        endpoint,
        previous=_token_from_login(login),
    )
    refreshed = apply_refreshed_token(login, token)
    if persist:
        save_refreshed_login(refreshed)
    return refreshed
