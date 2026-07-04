from __future__ import annotations

import asyncio
import base64
import json
import time
from pathlib import Path
from typing import Any

import ai
import httpx
import pytest

from js import codex_auth, codex_provider, login_cli, logins, model_client, providers


def _fake_jwt(account_id: str = "acct_123", email: str = "king@example.test") -> str:
    header = {"alg": "none"}
    payload = {
        "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
        "https://api.openai.com/profile": {"email": email},
    }

    def enc(obj: dict[str, Any]) -> str:
        raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    return f"{enc(header)}.{enc(payload)}.sig"


def test_codex_token_profile_extracts_account_and_email():
    assert codex_auth.token_profile(_fake_jwt()) == ("acct_123", "king@example.test")


def test_codex_login_round_trips_private_oauth_fields(tmp_path: Path):
    logins.set_config_dir(tmp_path)
    try:
        login = logins.Login(
            provider_id="openai-codex",
            sdk_provider_id="openai-codex",
            provider_base_url="https://chatgpt.com/backend-api",
            provider_api_key=_fake_jwt(),
            codex_refresh_token="refresh-token",
            codex_token_expires=time.time() + 3600,
            codex_account_id="acct_123",
            codex_email="king@example.test",
        )
        logins.save_login(login)
        loaded = logins.load_logins()["openai-codex"]
        assert loaded.effective_provider_id == "openai-codex"
        assert loaded.codex_refresh_token == "refresh-token"
        assert loaded.codex_account_id == "acct_123"
        assert loaded.codex_email == "king@example.test"
        assert (logins.login_path().stat().st_mode & 0o777) == 0o600
    finally:
        logins.set_config_dir(Path.home() / ".config" / "js")


def test_login_cli_lists_and_dispatches_codex_oauth(monkeypatch):
    rows = {p.id: p.display_name for p in providers.all_providers()}
    assert rows["openai-codex"].startswith("OpenAI Codex OAuth")
    # The headless device flow is exposed as an alias of the codex provider.
    assert providers.normalize_provider_id("openai-codex-device") == "openai-codex"

    calls: list[str] = []

    def fake_run(provider_id: str) -> int:
        calls.append(provider_id)
        return 0

    monkeypatch.setattr(login_cli, "_run_codex_login", fake_run)
    # The device alias must reach _run_codex_login unchanged so login_device() runs.
    assert login_cli.main(["openai-codex-device"]) == 0
    assert calls == ["openai-codex-device"]


def test_save_refreshed_login_degrades_gracefully_on_corrupt_logins_file(monkeypatch, capsys):
    # A background token refresh (codex_provider._ensure_access, or
    # ensure_fresh_login/_async here) must never crash an in-flight turn just
    # because logins.toml is corrupt — it should warn once and move on.
    def boom(_login):
        raise logins.LoginsCorruptError("logins.toml is broken")

    monkeypatch.setattr(logins, "save_login", boom)
    refreshed = codex_auth.login_from_token(
        codex_auth.CodexToken(access=_fake_jwt(), refresh="r", expires_at=time.time() + 3600)
    )
    codex_auth.save_refreshed_login(refreshed)  # must not raise
    assert "logins.toml is broken" in capsys.readouterr().err


def test_callback_redirect_uri_is_unchanged():
    # The redirect string must match what CLIENT_ID is registered with at
    # OpenAI — any bind-side fix has to leave this exactly alone.
    assert codex_auth.CALLBACK_REDIRECT_URI == "http://localhost:1455/auth/callback"


def test_bind_callback_servers_binds_available_loopback_families():
    servers = codex_auth._bind_callback_servers(codex_auth._CallbackHandler)
    try:
        assert servers  # at least 127.0.0.1 must be available in CI/sandboxes
        for server in servers:
            assert server.server_address[1] == codex_auth.CALLBACK_PORT
    finally:
        for server in servers:
            server.server_close()


def test_bind_callback_servers_degrades_when_v6_unavailable(monkeypatch):
    # Simulate a host with no IPv6 loopback: the v6 bind attempt raises, and
    # binding still succeeds on 127.0.0.1 alone instead of failing outright.
    def boom(self, *args, **kwargs):
        raise OSError("Address family not supported by protocol")

    monkeypatch.setattr(codex_auth._CallbackServerV6, "__init__", boom)
    servers = codex_auth._bind_callback_servers(codex_auth._CallbackHandler)
    try:
        assert len(servers) == 1
        assert not isinstance(servers[0], codex_auth._CallbackServerV6)
    finally:
        for server in servers:
            server.server_close()


def test_bind_callback_servers_returns_empty_when_both_families_fail(monkeypatch):
    def boom_v4(self, *args, **kwargs):
        raise OSError("port in use")

    def boom_v6(self, *args, **kwargs):
        raise OSError("Address family not supported by protocol")

    monkeypatch.setattr(codex_auth._CallbackServer, "__init__", boom_v4)
    monkeypatch.setattr(codex_auth._CallbackServerV6, "__init__", boom_v6)
    assert codex_auth._bind_callback_servers(codex_auth._CallbackHandler) == []


def test_stream_model_shapes_codex_params_without_output_cap(monkeypatch):
    class FakeExecutor:
        request = None

        async def _do_stream(self, request):
            self.request = request
            yield ai.events.StreamStart()
            yield ai.events.TextStart(block_id="text")
            yield ai.events.TextDelta(block_id="text", chunk="ok")
            yield ai.events.TextEnd(block_id="text")
            yield ai.events.StreamEnd()

    executor = FakeExecutor()

    def fake_resolve(model_id, *, provider_id, provider_base_url, provider_api_key, provider_headers=None):
        return ai.Model(id=model_id, provider=ai.get_provider("openai", api_key="x"))

    monkeypatch.setattr(model_client, "resolve_model", fake_resolve)
    model_client.stream_model(
        model_id="gpt-5-codex",
        provider_id="openai-codex",
        provider_base_url="https://chatgpt.com/backend-api",
        provider_api_key=_fake_jwt(),
        messages=[ai.user_message("hi")],
        tools=None,
        max_output_tokens=64,
        reasoning_effort="xhigh",
        on_text=lambda _s: None,
        executor=executor,
    )
    # Codex takes a reasoning effort but no output cap (its API rejects max_tokens).
    params = executor.request.params
    assert params.output is None
    assert params.reasoning.effort == "xhigh"


def test_stream_model_drives_codex_provider_end_to_end(monkeypatch):
    # The whole chain through stream_model: resolve_model builds the codex
    # provider, ai.stream's default executor calls provider.stream, and the
    # Responses SSE shape is decoded back to text — no executor stub.
    client = _FakeCodexHttpClient()

    def fake_provider_from_login_or_token(*, provider_base_url, provider_api_key):
        return codex_provider.OpenAICodexProvider(
            access_token=_fake_jwt(), account_id="acct_123",
            base_url="https://chatgpt.com/backend-api", client=client,
        )

    monkeypatch.setattr(
        codex_provider, "provider_from_login_or_token", fake_provider_from_login_or_token
    )
    chunks: list[str] = []
    result = model_client.stream_model(
        model_id="gpt-5.5",
        provider_id="openai-codex",
        provider_base_url="https://chatgpt.com/backend-api",
        provider_api_key=_fake_jwt(),
        messages=[ai.user_message("ping")],
        tools=None,
        max_output_tokens=64,
        reasoning_effort="max",  # codex has no 'max' stop -> dial snaps to 'xhigh'
        on_text=chunks.append,
    )
    assert result.text == "hello"
    assert result.finish_reason == "stop"
    assert "".join(chunks) == "hello"
    body = client.stream_calls[0]["json"]
    assert body["reasoning"] == {"effort": "xhigh", "summary": "detailed"}
    assert "max_tokens" not in body  # codex rejects output caps


class _FakeModelListResponse:
    status_code = 200

    def json(self):
        return {
            "models": [
                {"slug": "gpt-5-codex", "supported_in_api": True},
                {"id": "internal-only", "supported_in_api": False},
            ]
        }


class _FakeStreamResponse:
    status_code = 200

    async def aiter_lines(self):
        events = [
            {"type": "response.output_text.delta", "delta": "hello"},
            {
                "type": "response.completed",
                "response": {
                    "usage": {
                        "input_tokens": 3,
                        "output_tokens": 2,
                        "output_tokens_details": {"reasoning_tokens": 1},
                    }
                },
            },
        ]
        for event in events:
            yield f"event: {event['type']}"
            yield f"data: {json.dumps(event)}"
            yield ""


class _FakeStreamContext:
    def __init__(self, response: _FakeStreamResponse):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeCodexHttpClient:
    def __init__(self):
        self.get_calls: list[dict[str, Any]] = []
        self.stream_calls: list[dict[str, Any]] = []

    async def get(self, url, *, params=None, headers=None):
        self.get_calls.append({"url": url, "params": params, "headers": headers})
        return _FakeModelListResponse()

    def stream(self, method, url, *, headers=None, json=None):
        self.stream_calls.append({"method": method, "url": url, "headers": headers, "json": json})
        return _FakeStreamContext(_FakeStreamResponse())

    async def aclose(self):
        return None


async def _collect_provider_stream(provider: codex_provider.OpenAICodexProvider):
    events = []
    tools = [
        ai.types.tools.Tool(
            kind="function",
            name="fs_read",
            spec=ai.types.tools.ToolSpec(description="read", params={"type": "object"}),
        )
    ]
    from ai.models.core import params as ai_params

    async for event in provider.stream(
        ai.Model(id="gpt-5-codex", provider=provider),
        [ai.system_message("system"), ai.user_message("hello")],
        tools=tools,
        params=ai_params.InferenceRequestParams(
            output=ai_params.OutputParams(max_tokens=64),
            reasoning=ai_params.ReasoningParams(effort="high"),
        ),
    ):
        events.append(event)
    return events


def test_codex_provider_lists_models_and_streams_responses_shape():
    client = _FakeCodexHttpClient()
    provider = codex_provider.OpenAICodexProvider(
        access_token=_fake_jwt(),
        account_id="acct_123",
        base_url="https://chatgpt.com/backend-api",
        client=client,
    )

    models = asyncio.run(provider.list_models())
    assert "gpt-5-codex" in models
    assert "gpt-5.5" in models
    list_call = client.get_calls[0]
    assert list_call["url"].endswith("/codex/models")
    assert list_call["headers"]["Authorization"].startswith("Bearer ")
    assert list_call["headers"]["chatgpt-account-id"] == "acct_123"

    events = asyncio.run(_collect_provider_stream(provider))
    stream_call = client.stream_calls[0]
    assert stream_call["method"] == "POST"
    assert stream_call["url"].endswith("/codex/responses")
    assert stream_call["headers"]["OpenAI-Beta"] == "responses=experimental"
    body = stream_call["json"]
    assert body["store"] is False
    assert body["stream"] is True
    assert body["instructions"] == "system"
    assert body["tools"][0]["name"] == "fs_read"
    assert body["reasoning"] == {"effort": "high", "summary": "detailed"}
    assert "max_tokens" not in body
    assert any(isinstance(event, ai.events.TextDelta) and event.chunk == "hello" for event in events)
    assert isinstance(events[-1], ai.events.StreamEnd)
    assert events[-1].usage.output_tokens == 2

def _bare_jwt() -> str:
    def enc(obj: dict[str, Any]) -> str:
        raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    return f"{enc({'alg': 'none'})}.{enc({})}.sig"


class _StaticModelListResponse:
    def __init__(self, status_code: int, payload: object):
        self.status_code = status_code
        self._payload = payload
        self.request = httpx.Request("GET", "https://chatgpt.com/backend-api/codex/models")

    def json(self):
        return self._payload

    @property
    def text(self) -> str:
        return json.dumps(self._payload)


class _StaticGetClient:
    def __init__(self, responses: list[_StaticModelListResponse]):
        self._responses = responses
        self.get_calls: list[str] = []

    async def get(self, url, *, params=None, headers=None):
        self.get_calls.append(url)
        return self._responses[min(len(self.get_calls) - 1, len(self._responses) - 1)]

    async def aclose(self):
        return None


def _provider_with_client(client) -> codex_provider.OpenAICodexProvider:
    return codex_provider.OpenAICodexProvider(
        access_token=_fake_jwt(),
        account_id="acct_123",
        base_url="https://chatgpt.com/backend-api",
        client=client,
    )


def test_codex_model_list_includes_live_ids_without_allowlist():
    # The endpoint advertises ids beyond the historical two-model set; every
    # api-usable id must come through, including freshly shipped models.
    payload = {
        "models": [
            {"slug": "gpt-5-codex", "supported_in_api": True},
            {"id": "gpt-5.5", "supported_in_api": True},
            {"slug": "gpt-6-preview"},  # no flag -> treated as usable
            {"id": "internal-only", "supported_in_api": False},  # filtered out
        ]
    }
    client = _StaticGetClient([_StaticModelListResponse(200, payload)])
    models = asyncio.run(_provider_with_client(client).list_models())
    assert "gpt-5.5" in models
    assert "gpt-5-codex" in models
    assert "gpt-6-preview" in models
    assert "internal-only" not in models
    assert models == sorted(models)


def test_codex_model_list_falls_through_to_second_path_and_reports_errors():
    # First path 400s, second path returns models -> still succeeds.
    bad = _StaticModelListResponse(400, {"error": {"message": "no codex models here"}})
    good = _StaticModelListResponse(200, {"data": [{"id": "gpt-5.5"}]})
    client = _StaticGetClient([bad, good])
    models = asyncio.run(_provider_with_client(client).list_models())
    assert models == ["gpt-5.5"]

    # Both paths fail -> error names the paths and carries the bodies for debug.
    client2 = _StaticGetClient([_StaticModelListResponse(403, {"error": {"message": "forbidden"}})])
    with pytest.raises(ai.ProviderResponseError) as excinfo:
        asyncio.run(_provider_with_client(client2).list_models())
    err = excinfo.value
    assert "HTTP 403" in str(err)
    assert "forbidden" in str(err)
    assert err.body  # path error details attached to the provider error body


class _ErrorStreamResponse:
    status_code = 400

    def __init__(self, body: dict[str, Any]):
        self._body = body
        self._read = False
        self.request = httpx.Request("POST", "https://chatgpt.com/backend-api/codex/responses")

    async def aread(self) -> bytes:
        self._read = True
        return json.dumps(self._body).encode("utf-8")

    def json(self):
        if not self._read:
            raise httpx.ResponseNotRead()
        return self._body

    @property
    def text(self) -> str:
        if not self._read:
            raise httpx.ResponseNotRead()
        return json.dumps(self._body)


class _ErrorStreamClient:
    def __init__(self, body: dict[str, Any]):
        self._body = body

    def stream(self, method, url, *, headers=None, json=None):
        return _FakeStreamContext(_ErrorStreamResponse(self._body))

    async def aclose(self):
        return None


def test_codex_stream_400_names_model_and_captures_body():
    body = {"error": {"message": "the requested model is unavailable", "code": "model_not_found", "type": "invalid_request_error"}}
    provider = _provider_with_client(_ErrorStreamClient(body))

    async def drain():
        async for _ in provider.stream(
            ai.Model(id="gpt-5.5", provider=provider),
            [ai.user_message("hello")],
            tools=None,
            params=None,
        ):
            pass

    with pytest.raises(ai.ProviderError) as excinfo:
        asyncio.run(drain())
    err = excinfo.value
    # The model id is surfaced even though the API message did not include it,
    # and the raw response body is captured for the debug/provider error body.
    assert "gpt-5.5" in str(err)
    assert err.body == body
    assert err.code == "model_not_found"


class _IncompleteStreamResponse:
    status_code = 200

    async def aiter_lines(self):
        events = [
            {"type": "response.output_text.delta", "delta": "partial"},
            {
                "type": "response.incomplete",
                "response": {
                    "usage": {"input_tokens": 5, "output_tokens": 4},
                    "incomplete_details": {"reason": "max_output_tokens"},
                },
            },
        ]
        for event in events:
            yield f"event: {event['type']}"
            yield f"data: {json.dumps(event)}"
            yield ""


class _IncompleteStreamClient:
    def stream(self, method, url, *, headers=None, json=None):
        return _FakeStreamContext(_IncompleteStreamResponse())

    async def aclose(self):
        return None


def test_codex_stream_incomplete_marks_provider_metadata_instead_of_looking_clean():
    # A truncated turn must not look like a normal stop: the partial text
    # still comes through, but the StreamEnd carries the incomplete reason
    # instead of silently dropping incomplete_details on the floor.
    provider = _provider_with_client(_IncompleteStreamClient())

    async def drain():
        events = []
        async for event in provider.stream(
            ai.Model(id="gpt-5.5", provider=provider),
            [ai.user_message("hello")],
            tools=None,
            params=None,
        ):
            events.append(event)
        return events

    events = asyncio.run(drain())
    assert any(isinstance(e, ai.events.TextDelta) and e.chunk == "partial" for e in events)
    end = events[-1]
    assert isinstance(end, ai.events.StreamEnd)
    assert end.provider_metadata == {"incomplete": True, "incomplete_reason": "max_output_tokens"}
    assert end.usage.output_tokens == 4


def test_apply_refreshed_token_preserves_headers_and_rotates_only_token_fields():
    # login_from_token() only fills the codex/token fields; replace()ing its
    # result (the old behavior) reset provider_headers — and any other field
    # — to empty on every ~hourly refresh. apply_refreshed_token() must start
    # from the existing login instead.
    login = logins.Login(
        provider_id="openai-codex",
        sdk_provider_id="openai-codex",
        provider_base_url="https://chatgpt.com/backend-api",
        provider_api_key="old-access",
        provider_headers={"x-custom": "1"},
        codex_refresh_token="old-refresh",
        codex_token_expires=100.0,
        codex_account_id="acct_old",
        codex_email="old@example.test",
    )
    token = codex_auth.CodexToken(
        access="new-access", refresh="new-refresh", expires_at=200.0,
        account_id="acct_new", email="new@example.test",
    )
    refreshed = codex_auth.apply_refreshed_token(login, token)
    assert refreshed.provider_headers == {"x-custom": "1"}
    assert refreshed.provider_id == "openai-codex"
    assert refreshed.sdk_provider_id == "openai-codex"
    assert refreshed.provider_base_url == "https://chatgpt.com/backend-api"
    assert refreshed.provider_api_key == "new-access"
    assert refreshed.codex_refresh_token == "new-refresh"
    assert refreshed.codex_token_expires == 200.0
    assert refreshed.codex_account_id == "acct_new"
    assert refreshed.codex_email == "new@example.test"


def test_apply_refreshed_token_falls_back_to_default_base_url_when_missing():
    login = logins.Login(provider_id="openai-codex", provider_api_key="old", codex_refresh_token="r", codex_token_expires=0.0)
    token = codex_auth.CodexToken(access="new", refresh="new-r", expires_at=1.0, account_id="acct")
    refreshed = codex_auth.apply_refreshed_token(login, token)
    assert refreshed.provider_base_url == codex_auth.DEFAULT_CODEX_BASE_URL


def test_refreshed_login_preserves_provider_headers(monkeypatch):
    login = logins.Login(
        provider_id="openai-codex",
        sdk_provider_id="openai-codex",
        provider_base_url="https://chatgpt.com/backend-api",
        provider_api_key="old-access",
        provider_headers={"x-custom": "1"},
        codex_refresh_token="old-refresh",
        codex_token_expires=0.0,
        codex_account_id="acct_old",
    )
    new_token = codex_auth.CodexToken(access="new-access", refresh="new-refresh", expires_at=999.0, account_id="acct_new")
    monkeypatch.setattr(codex_auth, "refresh_token", lambda *a, **k: new_token)
    refreshed = codex_auth.refreshed_login(login)
    assert refreshed.provider_headers == {"x-custom": "1"}
    assert refreshed.provider_api_key == "new-access"
    assert refreshed.codex_refresh_token == "new-refresh"


def test_refreshed_login_async_preserves_provider_headers(monkeypatch):
    login = logins.Login(
        provider_id="openai-codex",
        sdk_provider_id="openai-codex",
        provider_base_url="https://chatgpt.com/backend-api",
        provider_api_key="old-access",
        provider_headers={"x-custom": "1"},
        codex_refresh_token="old-refresh",
        codex_token_expires=0.0,
    )
    new_token = codex_auth.CodexToken(access="new-access", refresh="new-refresh", expires_at=999.0, account_id="acct_new")

    async def fake_refresh_async(*a, **k):
        return new_token

    monkeypatch.setattr(codex_auth, "refresh_token_async", fake_refresh_async)
    refreshed = asyncio.run(codex_auth.refreshed_login_async(login))
    assert refreshed.provider_headers == {"x-custom": "1"}
    assert refreshed.provider_api_key == "new-access"


class _FakeTokenRefreshClient:
    """Serves the OAuth token-refresh POST that _ensure_access() triggers."""

    def __init__(self, token_payload: dict[str, Any]):
        self._token_payload = token_payload
        self.post_calls: list[dict[str, Any]] = []

    async def post(self, url, *, data=None, headers=None):
        self.post_calls.append({"url": url, "data": data, "headers": headers})
        return _StaticModelListResponse(200, self._token_payload)

    async def aclose(self):
        return None


def test_ensure_access_refresh_preserves_login_headers_end_to_end(tmp_path: Path):
    # The full codex_provider._ensure_access() path: an expired token forces a
    # background refresh mid-turn, and the persisted login must still carry
    # provider_headers afterward instead of being silently reset to {}.
    logins.set_config_dir(tmp_path)
    try:
        new_access = _fake_jwt(account_id="acct_new", email="new@example.test")
        client = _FakeTokenRefreshClient({"access_token": new_access, "refresh_token": "new-refresh", "expires_in": 3600})
        login = logins.Login(
            provider_id="openai-codex",
            sdk_provider_id="openai-codex",
            provider_base_url="https://chatgpt.com/backend-api",
            provider_api_key=_fake_jwt(),
            provider_headers={"x-custom": "1"},
            codex_refresh_token="old-refresh",
            codex_token_expires=0.0,  # already expired -> forces a refresh
            codex_account_id="acct_123",
        )
        provider = codex_provider.OpenAICodexProvider(
            access_token=login.provider_api_key,
            refresh_token=login.codex_refresh_token,
            expires_at=login.codex_token_expires,
            account_id=login.codex_account_id,
            base_url=login.provider_base_url,
            login=login,
            client=client,
        )
        asyncio.run(provider._ensure_access())
        assert client.post_calls  # the refresh actually happened
        saved = logins.load_logins()["openai-codex"]
        assert saved.provider_headers == {"x-custom": "1"}
        assert saved.provider_id == "openai-codex"
        assert saved.sdk_provider_id == "openai-codex"
        assert saved.provider_api_key == new_access
        assert saved.codex_refresh_token == "new-refresh"
    finally:
        logins.set_config_dir(Path.home() / ".config" / "js")


def test_codex_refresh_preserves_refresh_token_and_profile():
    previous = codex_auth.CodexToken(
        access="old-access",
        refresh="keep-this-refresh",
        expires_at=0.0,
        account_id="acct_123",
        email="king@example.test",
    )
    # Refresh response omits refresh_token (no rotation this round).
    data = {"access_token": _fake_jwt(), "expires_in": 3600}
    token = codex_auth._token_from_response(data, previous=previous)
    assert token.refresh == "keep-this-refresh"
    assert token.account_id == "acct_123"
    assert token.email == "king@example.test"


def test_codex_refresh_falls_back_to_previous_profile_when_jwt_is_bare():
    previous = codex_auth.CodexToken(
        access="old-access",
        refresh="r1",
        expires_at=0.0,
        account_id="acct_xyz",
        email="prev@example.test",
    )
    data = {"access_token": _bare_jwt(), "refresh_token": "r2", "expires_in": 60}
    token = codex_auth._token_from_response(data, previous=previous)
    assert token.refresh == "r2"
    assert token.account_id == "acct_xyz"  # carried from previous when JWT lacks it
    assert token.email == "prev@example.test"

    # Without a previous snapshot, a bare JWT still fails loudly.
    with pytest.raises(RuntimeError):
        codex_auth._token_from_response(data)
