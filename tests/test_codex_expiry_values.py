from __future__ import annotations

import pytest

from js import codex_auth


@pytest.fixture(autouse=True)
def fixed_clock_and_profile(monkeypatch):
    monkeypatch.setattr(codex_auth.time, "time", lambda: 1000.0)
    monkeypatch.setattr(codex_auth, "token_profile", lambda _access: ("fixture-account", "fixture@example.test"))


@pytest.mark.parametrize("expires_in, seconds", [
    (3600, 3600), (3600.5, 3600.5), ("3600", 3600), (" 3600 ", 3600),
    ("1e3", 1000), ("0.5", 0.5), (0.25, 0.25),
])
def test_valid_token_expiry_accepts_numeric_strings_and_numbers(expires_in, seconds):
    token = codex_auth._token_from_response({"access_token": "fixture-access", "refresh_token": "fixture-refresh", "expires_in": expires_in})
    assert token.expires_at == 1000 + seconds
    assert token.access == "fixture-access"
    assert token.refresh == "fixture-refresh"
    assert token.account_id == "fixture-account"


@pytest.mark.parametrize("expires_in", [
    True, False, None, "", "invalid", [], {}, 0, -1, "0", "-1", float("nan"),
    float("inf"), -float("inf"), "NaN", "Infinity", "-Infinity", "1e999", 10 ** 1000,
], ids=["true", "false", "none", "empty", "text", "list", "object", "zero", "negative", "zero-string", "negative-string", "nan", "inf", "minus-inf", "nan-string", "inf-string", "minus-inf-string", "overflow-string", "overflow-int"])
def test_invalid_token_expiry_has_existing_runtime_error(expires_in):
    with pytest.raises(RuntimeError):
        codex_auth._token_from_response({"access_token": "fixture-access", "refresh_token": "fixture-refresh", "expires_in": expires_in})


def test_missing_token_expiry_remains_runtime_error():
    with pytest.raises(RuntimeError):
        codex_auth._token_from_response({"access_token": "fixture-access", "refresh_token": "fixture-refresh"})


def test_computed_expiry_must_remain_finite(monkeypatch):
    monkeypatch.setattr(codex_auth.time, "time", lambda: 1e308)
    with pytest.raises(RuntimeError):
        codex_auth._token_from_response({"access_token": "fixture-access", "refresh_token": "fixture-refresh", "expires_in": 1e308})


def test_string_expiry_preserves_refresh_and_profile_fallback(monkeypatch):
    previous = codex_auth.CodexToken(access="old", refresh="old-refresh", expires_at=0, account_id="old-account", email="old@example.test")
    monkeypatch.setattr(codex_auth, "token_profile", lambda _access: (None, None))
    token = codex_auth._token_from_response({"access_token": "new-access", "expires_in": "3600"}, previous=previous)
    assert token.expires_at == 4600
    assert token.refresh == previous.refresh
    assert token.account_id == previous.account_id
    assert token.email == previous.email
