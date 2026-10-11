"""Codex device-code polling (ALL-CLAIMS codex_auth.py:86 LOW, RFC 8628 §3.5):
a 429 or ``slow_down`` means keep waiting, slower; only a real error ends the
login.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from js import codex_auth


class _ScriptedClient:
    """Serves the usercode POST, then the scripted poll responses in order."""

    def __init__(self, polls: list[tuple[int, dict[str, Any], dict[str, str]]]):
        self.polls = list(polls)
        self.poll_count = 0

    def __enter__(self):
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def post(self, url: str, **_: Any) -> httpx.Response:
        if url == codex_auth.DEVICE_USERCODE_URL:
            return _response(200, {"device_auth_id": "dev-1", "user_code": "ABCD-EFGH", "interval": 1})
        assert url == codex_auth.DEVICE_TOKEN_URL
        self.poll_count += 1
        status, body, headers = self.polls.pop(0)
        return _response(status, body, headers)


def _response(status: int, body: dict[str, Any], headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, content=json.dumps(body).encode(), headers={"Content-Type": "application/json", **(headers or {})})


@pytest.fixture
def harness(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(codex_auth.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(codex_auth.webbrowser, "open", lambda _: False)
    monkeypatch.setattr(codex_auth, "exchange_code_for_token", lambda code, verifier, uri: f"token:{code}:{verifier}")
    monkeypatch.setattr(codex_auth, "login_from_token", lambda token: token)
    return sleeps


def _run(monkeypatch, polls):
    client = _ScriptedClient(polls)
    monkeypatch.setattr(codex_auth.httpx, "Client", lambda **_: client)
    return client, codex_auth.login_device(open_browser=False)


def test_429_and_slow_down_keep_polling_slower_instead_of_failing(monkeypatch, harness):
    done = (200, {"authorization_code": "code-1", "code_verifier": "ver-1"}, {})
    client, result = _run(
        monkeypatch,
        [
            (403, {}, {}),  # pending, as today
            (429, {"error": "slow_down"}, {}),  # RFC 8628: keep waiting, +5s
            (400, {"error": "slow_down"}, {}),  # some servers say it with a 400
            (429, {}, {"Retry-After": "30"}),  # plain rate limit with a hint
            (400, {"error": "authorization_pending"}, {}),
            done,
        ],
    )
    assert result == "token:code-1:ver-1"
    assert client.poll_count == 6
    # Interval: 1+3 margin = 4 → after slow_down 9 → 14 → Retry-After 30 wins over 19 → stays 30.
    assert harness[1:] == [4.0, 9.0, 14.0, 30.0, 30.0]


def test_a_real_error_still_ends_the_login(monkeypatch, harness):
    with pytest.raises(RuntimeError, match="device token polling failed"):
        _run(monkeypatch, [(403, {}, {}), (500, {"error": {"message": "boom"}}, {})])


def test_access_denied_ends_the_login(monkeypatch, harness):
    with pytest.raises(RuntimeError, match="access_denied"):
        _run(monkeypatch, [(400, {"error": "access_denied"}, {})])


def test_retry_after_garbage_is_ignored():
    response = _response(429, {}, {"Retry-After": "soon"})
    assert codex_auth._device_poll_pending(response, 4.0) == (True, 9.0)
    response = _response(429, {}, {"Retry-After": "99999"})
    assert codex_auth._device_poll_pending(response, 4.0) == (True, 9.0)
