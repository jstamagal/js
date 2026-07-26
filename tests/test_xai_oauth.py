"""xAI OAuth login: discovery, PKCE authorize URL, exchange, refresh."""

from __future__ import annotations

import base64
import json
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from js import logins, providers, routing, xai_auth


class _Response:
    def __init__(self, status_code: int, payload: Any):
        self.status_code = status_code
        self.text = payload if isinstance(payload, str) else json.dumps(payload)


class _FakeClient:
    def __init__(self, get_response: _Response | None = None, post_response: _Response | None = None):
        self._get_response = get_response
        self._post_response = post_response
        self.get_calls: list[dict[str, Any]] = []
        self.post_calls: list[dict[str, Any]] = []

    def get(self, url, *, headers=None):
        self.get_calls.append({"url": url, "headers": headers})
        assert self._get_response is not None
        return self._get_response

    def post(self, url, *, data=None, headers=None):
        self.post_calls.append({"url": url, "data": data, "headers": headers})
        assert self._post_response is not None
        return self._post_response

    def close(self):
        return None


def _id_token(claims: dict[str, Any]) -> str:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{body}.signature"


def test_discovery_returns_endpoints_from_the_document():
    client = _FakeClient(
        get_response=_Response(
            200,
            {
                "authorization_endpoint": "https://auth.x.ai/oauth/authorize",
                "token_endpoint": "https://auth.x.ai/oauth/token",
            },
        )
    )
    endpoints = xai_auth.discover(client=client)
    assert endpoints.authorize == "https://auth.x.ai/oauth/authorize"
    assert endpoints.token == "https://auth.x.ai/oauth/token"
    assert client.get_calls[0]["url"] == xai_auth.DISCOVERY_URL


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://auth.x.ai/oauth/authorize",  # not HTTPS
        "https://auth.evil.example/oauth/authorize",  # off x.ai
        "",
    ],
)
def test_discovery_refuses_an_endpoint_that_would_send_the_code_elsewhere(endpoint: str):
    client = _FakeClient(
        get_response=_Response(200, {"authorization_endpoint": endpoint, "token_endpoint": "https://auth.x.ai/t"})
    )
    with pytest.raises(RuntimeError):
        xai_auth.discover(client=client)


def test_authorize_url_carries_pkce_and_loopback_redirect():
    url = xai_auth.build_authorize_url(
        "https://auth.x.ai/oauth/authorize", "state-value", "nonce-value", "challenge-value"
    )
    query = parse_qs(urlparse(url).query)
    assert query["response_type"] == ["code"]
    assert query["client_id"] == [xai_auth.CLIENT_ID]
    assert query["code_challenge"] == ["challenge-value"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["state"] == ["state-value"]
    assert query["nonce"] == ["nonce-value"]
    assert query["scope"] == [xai_auth.SCOPE]
    assert query["redirect_uri"] == [xai_auth.CALLBACK_REDIRECT_URI]


def test_code_exchange_builds_a_login_with_the_bearer_as_the_api_key():
    client = _FakeClient(
        post_response=_Response(
            200,
            {
                "access_token": "access-1",
                "refresh_token": "refresh-1",
                "expires_in": 3600,
                "id_token": _id_token({"email": "grok@example.com", "sub": "user-1"}),
            },
        )
    )
    token = xai_auth.exchange_code_for_token("https://auth.x.ai/oauth/token", "code-1", "verifier-1", client=client)
    assert token.access == "access-1"
    assert token.refresh == "refresh-1"
    assert token.email == "grok@example.com"

    sent = client.post_calls[0]["data"]
    assert sent["grant_type"] == "authorization_code"
    assert sent["code_verifier"] == "verifier-1"
    assert sent["redirect_uri"] == xai_auth.CALLBACK_REDIRECT_URI

    login = xai_auth.login_from_token(token)
    assert login.provider_id == xai_auth.XAI_PROVIDER_ID
    assert login.provider_api_key == "access-1"
    assert login.provider_base_url == xai_auth.DEFAULT_XAI_BASE_URL
    assert login.xai_refresh_token == "refresh-1"
    assert login.xai_token_endpoint == "https://auth.x.ai/oauth/token"


def test_code_exchange_without_a_refresh_token_is_an_error():
    client = _FakeClient(post_response=_Response(200, {"access_token": "access-1", "expires_in": 3600}))
    with pytest.raises(RuntimeError, match="refresh_token"):
        xai_auth.exchange_code_for_token("https://auth.x.ai/oauth/token", "code-1", "verifier-1", client=client)


def test_refresh_keeps_the_previous_refresh_token_when_the_response_omits_one():
    previous = xai_auth.XaiToken(
        access="old", refresh="refresh-1", expires_at=0.0, token_endpoint="https://auth.x.ai/oauth/token"
    )
    client = _FakeClient(post_response=_Response(200, {"access_token": "access-2", "expires_in": 3600}))
    token = xai_auth.refresh_token(
        "refresh-1", "https://auth.x.ai/oauth/token", previous=previous, client=client
    )
    assert token.access == "access-2"
    assert token.refresh == "refresh-1"
    assert client.post_calls[0]["data"]["grant_type"] == "refresh_token"


def test_refresh_preserves_operator_set_fields_on_the_login():
    login = xai_auth.login_from_token(
        xai_auth.XaiToken(
            access="old",
            refresh="refresh-1",
            expires_at=0.0,
            token_endpoint="https://auth.x.ai/oauth/token",
            email="grok@example.com",
        )
    )
    login.provider_headers["X-Custom"] = "kept"
    refreshed = xai_auth.apply_refreshed_token(
        login,
        xai_auth.XaiToken(
            access="new", refresh=None, expires_at=123.0, token_endpoint="https://auth.x.ai/oauth/token"
        ),
    )
    assert refreshed.provider_api_key == "new"
    assert refreshed.xai_refresh_token == "refresh-1"
    assert refreshed.xai_email == "grok@example.com"
    assert refreshed.provider_headers == {"X-Custom": "kept"}


def test_login_needs_refresh_tracks_expiry():
    now = time.time()
    fresh = xai_auth.login_from_token(
        xai_auth.XaiToken(access="a", refresh="r", expires_at=now + 3600, token_endpoint="https://auth.x.ai/t")
    )
    stale = xai_auth.login_from_token(
        xai_auth.XaiToken(access="a", refresh="r", expires_at=now + 10, token_endpoint="https://auth.x.ai/t")
    )
    assert xai_auth.login_needs_refresh(fresh, now=now) is False
    assert xai_auth.login_needs_refresh(stale, now=now) is True


def test_provider_is_registered_against_the_xai_endpoint():
    provider = providers.get_provider(xai_auth.XAI_PROVIDER_ID)
    assert provider is not None
    assert provider.default_base_url == xai_auth.DEFAULT_XAI_BASE_URL
    assert provider.transport == "openai"


def test_saved_login_round_trips_the_oauth_fields(tmp_path):
    logins.set_config_dir(tmp_path)
    try:
        logins.save_login(
            xai_auth.login_from_token(
                xai_auth.XaiToken(
                    access="access-1",
                    refresh="refresh-1",
                    expires_at=1234.0,
                    token_endpoint="https://auth.x.ai/oauth/token",
                    email="grok@example.com",
                )
            )
        )
        loaded = logins.load_logins()[xai_auth.XAI_PROVIDER_ID]
        assert loaded.xai_refresh_token == "refresh-1"
        assert loaded.xai_token_expires == 1234.0
        assert loaded.xai_token_endpoint == "https://auth.x.ai/oauth/token"
        assert loaded.xai_email == "grok@example.com"
    finally:
        logins.set_config_dir(None)


def test_routing_refreshes_an_expired_bearer_before_handing_it_out(tmp_path, monkeypatch):
    logins.set_config_dir(tmp_path)
    try:
        logins.save_login(
            xai_auth.login_from_token(
                xai_auth.XaiToken(
                    access="expired",
                    refresh="refresh-1",
                    expires_at=time.time() - 1,
                    token_endpoint="https://auth.x.ai/oauth/token",
                )
            )
        )

        def fake_refresh(refresh, endpoint, *, previous=None, client=None):
            assert refresh == "refresh-1"
            return xai_auth.XaiToken(
                access="renewed", refresh="refresh-1", expires_at=time.time() + 3600, token_endpoint=endpoint
            )

        monkeypatch.setattr(xai_auth, "refresh_token", fake_refresh)
        route = routing.resolve_model_route("grok-4", configured_provider_id=xai_auth.XAI_PROVIDER_ID)
        assert route.api_key == "renewed"
        assert logins.load_logins()[xai_auth.XAI_PROVIDER_ID].provider_api_key == "renewed"
    finally:
        logins.set_config_dir(None)
