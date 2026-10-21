from __future__ import annotations

import asyncio
import base64
import json
import socket
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
        logins.set_config_dir(Path.home() / ".js" / "logins")


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


def test_save_refreshed_login_degrades_gracefully_on_corrupt_logins_file(monkeypatch, capsys, tmp_path: Path):
    # A background token refresh (codex_provider._ensure_access, or
    # ensure_fresh_login/_async here) must never crash an in-flight turn just
    # because logins.toml is corrupt — it should warn once and move on.
    logins.set_config_dir(tmp_path)
    try:
        stored = logins.Login(provider_id="openai-codex", provider_api_key="old", codex_refresh_token="r", codex_token_expires=0.0)
        logins.save_login(stored)

        def boom(_login):
            raise logins.LoginsCorruptError("logins.toml is broken")

        monkeypatch.setattr(logins, "save_login", boom)
        refreshed = codex_auth.apply_refreshed_token(
            stored, codex_auth.CodexToken(access=_fake_jwt(), refresh="r2", expires_at=time.time() + 3600)
        )
        assert codex_auth.save_refreshed_login(refreshed, derived_from=stored) is False  # must not raise
        assert "logins.toml is broken" in capsys.readouterr().err
        # The rotated token is kept for this process (not re-posted later).
        assert logins.get_login("openai-codex").codex_refresh_token == "r2"
    finally:
        logins.set_config_dir(Path.home() / ".js" / "logins")


@pytest.mark.parametrize("prefix", ["", "not-a-url\n", "http://localhost:1455/auth/callback?code=bad&state=wrong\n"])
def test_browser_login_accepts_pasted_callback(monkeypatch, capsys, prefix):
    import os

    server = codex_auth._CallbackServer(("127.0.0.1", 0), codex_auth._CallbackHandler)
    monkeypatch.setattr(codex_auth, "_bind_callback_servers", lambda _: [server])
    monkeypatch.setattr(codex_auth.secrets, "token_urlsafe", lambda _: "expected-state")
    monkeypatch.setattr(codex_auth, "_pkce_pair", lambda: ("verifier", "challenge"))
    monkeypatch.setattr(codex_auth.webbrowser, "open", lambda _: False)
    calls = []
    monkeypatch.setattr(codex_auth, "exchange_code_for_token", lambda *args: calls.append(args) or "token")
    monkeypatch.setattr(codex_auth, "login_from_token", lambda token: token)
    reader, writer = os.pipe()
    with os.fdopen(reader, "r") as stdin:
        monkeypatch.setattr(codex_auth.sys, "stdin", stdin)
        os.write(writer, (prefix + "http://localhost:1455/auth/callback?code=test-code&state=expected-state\n").encode())
        os.close(writer)
        assert codex_auth.login_browser(timeout_s=2) == "token"
    assert calls == [("test-code", "verifier", codex_auth.CALLBACK_REDIRECT_URI)]
    assert server.fileno() == -1


def test_browser_login_eof_times_out_and_closes_listener(monkeypatch):
    import os

    server = codex_auth._CallbackServer(("127.0.0.1", 0), codex_auth._CallbackHandler)
    monkeypatch.setattr(codex_auth, "_bind_callback_servers", lambda _: [server])
    monkeypatch.setattr(codex_auth.webbrowser, "open", lambda _: False)
    reader, writer = os.pipe()
    os.close(writer)
    with os.fdopen(reader, "r") as stdin:
        monkeypatch.setattr(codex_auth.sys, "stdin", stdin)
        with pytest.raises(RuntimeError):
            codex_auth.login_browser(timeout_s=0.01)
    assert server.fileno() == -1


def test_callback_redirect_uri_is_unchanged():
    # The redirect string must match what CLIENT_ID is registered with at
    # OpenAI — any bind-side fix has to leave this exactly alone.
    assert codex_auth.CALLBACK_REDIRECT_URI == "http://localhost:1455/auth/callback"


def test_bind_callback_servers_binds_available_loopback_families(monkeypatch):
    monkeypatch.setattr(codex_auth, "CALLBACK_PORT", 0)
    servers = codex_auth._bind_callback_servers(codex_auth._CallbackHandler)
    try:
        assert servers  # at least 127.0.0.1 must be available in CI/sandboxes
        assert servers[0].address_family == socket.AF_INET
        for server in servers[1:]:
            assert server.address_family == socket.AF_INET6
    finally:
        for server in servers:
            server.server_close()


def test_bind_callback_servers_degrades_when_v6_unavailable(monkeypatch):
    # Simulate a host with no IPv6 loopback: the v6 bind attempt raises, and
    # binding still succeeds on 127.0.0.1 alone instead of failing outright.
    def boom(self, *args, **kwargs):
        raise OSError("Address family not supported by protocol")

    monkeypatch.setattr(codex_auth, "CALLBACK_PORT", 0)
    monkeypatch.setattr(codex_auth._CallbackServerV6, "__init__", boom)
    servers = codex_auth._bind_callback_servers(codex_auth._CallbackHandler)
    try:
        assert len(servers) == 1
        assert servers[0].address_family == socket.AF_INET
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
    class FakeStreamFactory:
        kwargs = None

        def __call__(self, **kwargs):
            self.kwargs = kwargs

            async def generate():
                yield ai.events.StreamStart()
                yield ai.events.TextStart(block_id="text")
                yield ai.events.TextDelta(block_id="text", chunk="ok")
                yield ai.events.TextEnd(block_id="text")
                yield ai.events.StreamEnd()

            return ai.models.Stream(generate())

    factory = FakeStreamFactory()

    def fake_resolve(model_id, *, provider_id, provider_base_url, provider_api_key, provider_headers=None):
        return ai.Model(id=model_id, provider=ai.get_provider("openai", api_key="x"))

    monkeypatch.setattr(model_client, "resolve_model", fake_resolve)
    monkeypatch.setattr(model_client, "_open_stream", factory)
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
    )
    # Codex takes a reasoning effort but no output cap (its API rejects max_tokens).
    params = factory.kwargs["params"]
    assert params.output is None
    assert params.reasoning.effort == "xhigh"


def test_stream_model_drives_codex_provider_end_to_end(monkeypatch):
    # The whole chain through stream_model: resolve_model builds the codex
    # provider, ai.stream's default executor calls provider.stream, and the
    # Responses SSE shape is decoded back to text — no executor stub.
    client = _FakeCodexHttpClient()

    def fake_provider_from_login_or_token(*, provider_base_url, provider_api_key, provider_headers=None):
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


def test_codex_provider_lists_models_and_streams_responses_shape(monkeypatch):
    monkeypatch.setattr(codex_provider, "_client_version", lambda *args: "0.145.0")
    client = _FakeCodexHttpClient()
    provider = codex_provider.OpenAICodexProvider(
        access_token=_fake_jwt(),
        account_id="acct_123",
        base_url="https://chatgpt.com/backend-api",
        client=client,
    )

    models = asyncio.run(provider.list_models())
    assert models == ["gpt-5-codex"]
    list_call = client.get_calls[0]
    assert list_call["url"].endswith("/codex/models")
    # the backend drops any model whose minimal_client_version outranks this,
    # so a stale value here silently hides newly released models
    assert list_call["params"]["client_version"] == "0.145.0"
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
    assert refreshed.provider_base_url == codex_auth.CODEX_BASE_URL


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
        logins.save_login(login)  # a refresh persists only a STORED login (never resurrects a logout)
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
        logins.set_config_dir(Path.home() / ".js" / "logins")


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


class _SSEResponse:
    status_code = 200

    def __init__(self, events):
        self._events = events

    async def aiter_lines(self):
        for event in self._events:
            yield f"data: {json.dumps(event)}"
            yield ""


class _SSEClient:
    def __init__(self, events):
        self._events = events

    def stream(self, method, url, *, headers=None, json=None):
        return _FakeStreamContext(_SSEResponse(self._events))

    async def aclose(self):
        return None


def _drain(events):
    provider = _provider_with_client(_SSEClient(events))

    async def run():
        stream = ai.models.Stream(provider.stream(
            ai.Model(id="gpt-5.5", provider=provider), [ai.user_message("hi")], tools=None, params=None))
        async for _ in stream:
            pass
        return stream

    return asyncio.run(run())


def _tool_args(stream):
    return [(part.tool_call_id, part.tool_args) for part in stream.message.parts
            if isinstance(part, ai.types.messages.ToolCallPart)]


def _call_item(item_id, call_id, name, arguments=None):
    item = {"id": item_id, "type": "function_call", "call_id": call_id, "name": name}
    if arguments is not None:
        item["arguments"] = arguments
    return item


def test_final_arguments_complete_partial_deltas():
    """ALL-CLAIMS codex_provider.py:104 (doc #491 HIGH): deltas `{"a":` then
    the authoritative done `{"a":1}` assembled to `{"a":` -- a truncated call."""
    stream = _drain([
        {"type": "response.output_item.added", "item": _call_item("item_A", "call_A", "f_a")},
        {"type": "response.function_call_arguments.delta", "item_id": "item_A", "delta": '{"a":'},
        {"type": "response.function_call_arguments.done", "item_id": "item_A", "arguments": '{"a":1}'},
        {"type": "response.output_item.done", "item": _call_item("item_A", "call_A", "f_a", '{"a":1}')},
        {"type": "response.completed", "response": {"status": "completed"}},
    ])
    assert _tool_args(stream) == [("call_A", '{"a":1}')]


def test_parallel_calls_with_interleaved_deltas_each_get_their_own_arguments():
    """ALL-CLAIMS codex_provider.py:101: deltas went to whichever call was
    'active', and a second ToolStart for call_A appeared with empty args."""
    stream = _drain([
        {"type": "response.output_item.added", "item": _call_item("item_A", "call_A", "f_a")},
        {"type": "response.function_call_arguments.delta", "item_id": "item_A", "delta": '{"a":'},
        {"type": "response.output_item.added", "item": _call_item("item_B", "call_B", "f_b")},
        {"type": "response.function_call_arguments.delta", "item_id": "item_B", "delta": '{"b":'},
        {"type": "response.function_call_arguments.done", "item_id": "item_B", "arguments": '{"b":2}'},
        {"type": "response.function_call_arguments.done", "item_id": "item_A", "arguments": '{"a":1}'},
        {"type": "response.output_item.done", "item": _call_item("item_A", "call_A", "f_a", '{"a":1}')},
        {"type": "response.output_item.done", "item": _call_item("item_B", "call_B", "f_b", '{"b":2}')},
        {"type": "response.completed", "response": {"status": "completed"}},
    ])
    assert _tool_args(stream) == [("call_A", '{"a":1}'), ("call_B", '{"b":2}')]


def test_a_call_with_no_deltas_takes_its_done_arguments():
    stream = _drain([
        {"type": "response.output_item.done", "item": _call_item("item_A", "call_A", "f_a", '{"x":true}')},
        {"type": "response.completed", "response": {"status": "completed"}},
    ])
    assert _tool_args(stream) == [("call_A", '{"x":true}')]


@pytest.mark.parametrize("event,message", [
    ({"type": "response.failed", "sequence_number": 12,
      "response": {"id": "r", "status": "failed",
                   "error": {"code": "server_error", "message": "boom: upstream overloaded"}}},
     "boom: upstream overloaded"),
    ({"type": "error", "code": "server_error", "message": "boom flat", "param": None}, "boom flat"),
])
def test_stream_errors_keep_their_message_and_code_so_retry_can_see_them(event, message):
    """ALL-CLAIMS codex_provider.py:93 HIGH: response.failed nests its error
    under `response`, a flat error has `code` at top level; both came out as
    'OpenAI Codex stream failed' with code None, so is_retryable was always
    False and a transient server_error was never retried."""
    with pytest.raises(ai.ProviderAPIError) as caught:
        _drain([event])
    assert caught.value.message == message
    assert caught.value.code == "server_error"
    assert caught.value.is_retryable is True


def test_a_stream_that_ends_without_a_terminal_event_raises_retryable():
    """ALL-CLAIMS codex_provider.py:97 (doc #372, ruhondo #378): a broken pipe
    used to end as a clean StreamEnd, so a cut answer or a half tool call
    passed as finished."""
    with pytest.raises(ai.ProviderResponseError) as caught:
        _drain([
            {"type": "response.output_item.added", "item": _call_item("item_A", "call_A", "write")},
            {"type": "response.function_call_arguments.delta", "item_id": "item_A", "delta": '{"path": "a'},
        ])
    assert caught.value.is_retryable is True


def test_the_owned_client_bounds_silence_not_total_time():
    """ALL-CLAIMS codex_provider.py:96: httpx.AsyncClient(timeout=None) let a
    stalled connection hang the turn forever."""
    provider = codex_provider.OpenAICodexProvider(access_token=_fake_jwt(), account_id="acct_123")
    try:
        timeout = provider.client.timeout
        assert timeout.read == codex_provider._READ_GAP_S and timeout.connect is not None
    finally:
        asyncio.run(provider.aclose())


def test_a_silent_server_times_out_as_a_retryable_provider_error(monkeypatch):
    import socket
    import threading

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    held = []

    def accept_and_stall():
        conn, _ = listener.accept()
        held.append(conn)                       # read the request, never answer

    threading.Thread(target=accept_and_stall, daemon=True).start()
    monkeypatch.setattr(codex_provider, "_CLIENT_TIMEOUT", httpx.Timeout(1.0, read=0.3))
    provider = codex_provider.OpenAICodexProvider(
        access_token=_fake_jwt(), account_id="acct_123",
        base_url=f"http://127.0.0.1:{listener.getsockname()[1]}")

    async def run():
        try:
            async for _ in provider.stream(ai.Model(id="gpt-5.5", provider=provider),
                                           [ai.user_message("hi")], tools=None, params=None):
                pass
        finally:
            await provider.aclose()

    with pytest.raises(ai.ProviderTimeoutError) as caught:
        asyncio.run(asyncio.wait_for(run(), 10))
    assert caught.value.is_retryable is True
    listener.close()


def test_list_models_maps_non_json_and_transport_failures():
    """ALL-CLAIMS codex_provider.py:99: a 200 non-JSON body raised a raw
    JSONDecodeError; transport errors were not mapped."""
    class NotJson:
        status_code = 200

        def json(self):
            raise json.JSONDecodeError("x", "<html>", 0)

    class Client:
        def __init__(self, outcome):
            self.outcome = outcome

        async def get(self, *a, **k):
            if isinstance(self.outcome, Exception):
                raise self.outcome
            return self.outcome

        async def aclose(self):
            return None

    with pytest.raises(ai.ProviderResponseError):
        asyncio.run(_provider_with_client(Client(NotJson())).list_models())
    with pytest.raises(ai.ProviderConnectionError):
        asyncio.run(_provider_with_client(Client(httpx.ConnectError("refused"))).list_models())
    with pytest.raises(ai.ProviderTimeoutError):
        asyncio.run(_provider_with_client(Client(httpx.ReadTimeout("slow"))).list_models())


def test_a_half_streamed_tool_call_never_reaches_the_caller(monkeypatch):
    """maggie #1153: through the real assembler (model_client._stream_async),
    a tool call cut by EOF raises before any ModelStreamResult exists, so
    nothing is dispatched."""
    import contextlib
    import types as _types

    from js import model_client

    events = [
        {"type": "response.output_item.added", "item": _call_item("item_A", "call_A", "write")},
        {"type": "response.function_call_arguments.delta", "item_id": "item_A", "delta": '{"path": "/etc/pass'},
    ]

    @contextlib.asynccontextmanager
    async def fake_open(**_kwargs):
        provider = _provider_with_client(_SSEClient(events))
        yield ai.models.Stream(provider.stream(
            ai.Model(id="gpt-5.5", provider=provider), [ai.user_message("hi")], tools=None, params=None))

    monkeypatch.setattr(model_client, "_open_stream", fake_open)
    model = _types.SimpleNamespace(id="gpt-5.5", provider=_types.SimpleNamespace(name="stub"))
    results = []

    async def run():
        results.append(await model_client._stream_async(model, [], None, None, on_text=lambda _s: None))

    with pytest.raises(ai.ProviderResponseError):
        asyncio.run(run())
    assert results == []


def test_codex_requests_carry_the_provider_headers(monkeypatch):
    from js import logins

    client = _FakeCodexHttpClient()
    monkeypatch.setattr(logins, "load_logins", lambda: {})
    real = codex_provider.OpenAICodexProvider.__init__

    def with_fake_client(self, **kwargs):
        real(self, **{**kwargs, "client": client})

    monkeypatch.setattr(codex_provider.OpenAICodexProvider, "__init__", with_fake_client)
    model_client.stream_model(
        model_id="gpt-5.5", provider_id="openai-codex",
        provider_base_url="https://chatgpt.com/backend-api", provider_api_key=_fake_jwt(),
        provider_headers={"X-JS-Scrub": "on", "X-Pool": "fofa"},
        messages=[ai.user_message("ping")], tools=None, max_output_tokens=None,
        reasoning_effort=None, on_text=lambda _c: None,
    )
    sent = client.stream_calls[0]["headers"]
    assert sent["X-JS-Scrub"] == "on" and sent["X-Pool"] == "fofa"


def test_saved_codex_login_keeps_the_requests_explicit_base_url(monkeypatch):
    login = codex_auth.login_from_token(codex_auth.CodexToken(
        access=_fake_jwt(), refresh="refresh", expires_at=time.time() + 3600,
        account_id="acct_123",
    ))
    logins.save_login(login)
    client = _FakeCodexHttpClient()
    initialize = codex_provider.OpenAICodexProvider.__init__

    def with_fake_client(self, **kwargs):
        initialize(self, **{**kwargs, "client": client})

    monkeypatch.setattr(codex_provider.OpenAICodexProvider, "__init__", with_fake_client)
    model_client.stream_model(
        model_id="gpt-5.5", provider_id="openai-codex",
        provider_base_url="https://codex.example.test/backend-api",
        provider_api_key=login.provider_api_key,
        messages=[ai.user_message("ping")], tools=None, max_output_tokens=None,
        reasoning_effort=None, on_text=lambda _c: None,
    )

    assert client.stream_calls[0]["url"] == "https://codex.example.test/backend-api/codex/responses"
    assert logins.load_logins()["openai-codex"].provider_base_url == login.provider_base_url


def test_codex_extra_params_reach_the_http_body(monkeypatch):
    client = _FakeCodexHttpClient()
    monkeypatch.setattr(
        codex_provider, "provider_from_login_or_token",
        lambda **_kwargs: _provider_with_client(client),
    )
    model_client.stream_model(
        model_id="gpt-5.5", provider_id="openai-codex",
        provider_base_url="https://chatgpt.com/backend-api", provider_api_key=_fake_jwt(),
        provider_extra={"extra_body": {"text": {"verbosity": "high"}}, "service_tier": "priority"},
        messages=[ai.user_message("ping")], tools=None, max_output_tokens=None,
        reasoning_effort=None, on_text=lambda _c: None,
    )

    body = client.stream_calls[0]["json"]
    assert body["text"] == {"verbosity": "high"}
    assert body["service_tier"] == "priority"


def test_codex_session_cache_key_reaches_the_http_body(monkeypatch):
    client = _FakeCodexHttpClient()
    monkeypatch.setattr(
        codex_provider, "provider_from_login_or_token",
        lambda **_kwargs: _provider_with_client(client),
    )
    model_client.stream_model(
        model_id="gpt-5.5", provider_id="openai-codex",
        provider_base_url="https://chatgpt.com/backend-api", provider_api_key=_fake_jwt(),
        cache_key="js-agent-session-1",
        messages=[ai.user_message("ping")], tools=None, max_output_tokens=None,
        reasoning_effort=None, on_text=lambda _c: None,
    )

    assert client.stream_calls[0]["json"]["prompt_cache_key"] == "js-agent-session-1"


def test_browser_oauth_accepts_input_and_notice_services(monkeypatch, capsys):
    from types import SimpleNamespace

    notices, opened, registered = [], [], []
    source = object()
    server = SimpleNamespace(server_close=lambda: None)
    selector = SimpleNamespace(
        register=lambda *args: registered.append(args),
        select=lambda **kwargs: [(SimpleNamespace(data=None, fd=7), None)],
        close=lambda: None,
    )
    monkeypatch.setattr(codex_auth, "_bind_callback_servers", lambda cls: [server])
    monkeypatch.setattr(codex_auth.selectors, "DefaultSelector", lambda: selector)
    monkeypatch.setattr(codex_auth.secrets, "token_urlsafe", lambda size: "state")
    monkeypatch.setattr(codex_auth, "exchange_code_for_token", lambda *args: "token")
    monkeypatch.setattr(codex_auth, "login_from_token", lambda token: token)
    result = codex_auth.login_browser(
        stdin=source, read=lambda *args: b"http://localhost:1455/auth/callback?code=code&state=state\n",
        say=lambda *args, **kwargs: notices.append(args), open_browser=False, opener=opened.append,
    )
    assert result == "token" and notices and opened == []
    assert any(args[0] is source for args in registered)
    assert capsys.readouterr().out == ""
