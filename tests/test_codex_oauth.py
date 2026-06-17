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
        return ai.Model(model_id, provider=ai.get_provider("openai", api_key="x"))

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
    assert executor.request.params == {"reasoning_effort": "xhigh"}


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
            args=ai.types.tools.FunctionToolArgs(description="read", params={"type": "object"}),
        )
    ]
    async for event in provider.stream(
        ai.Model("gpt-5-codex", provider=provider),
        [ai.system_message("system"), ai.user_message("hello")],
        tools=tools,
        params={"max_tokens": 64, "reasoning_effort": "high"},
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
            ai.Model("gpt-5.5", provider=provider),
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
