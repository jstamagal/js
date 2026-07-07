"""Custom `js --login` providers must be first-class routing citizens.

Regression for the FIXME cluster where a custom login (booty/testes/a local
llama.cpp box) was visible to /model and --list-models but invisible to
routing: `provider/model` prefixes shipped the WHOLE string to the default
provider, and pinning `provider.id` resolved base_url but handed the raw js id
to the SDK ("unknown provider id").
"""

from __future__ import annotations

import pytest

from js import logins, providers
from js.logins import Login
from js.routing import resolve_model_route

BASE_URL = "http://localhost:8050/v1"
GGUF = "/home/ronald_rump/.cache/huggingface/models/ornith-1.0-35b-Q5_K_M.gguf"


@pytest.fixture(autouse=True)
def isolated_login_store(monkeypatch, tmp_path):
    monkeypatch.setattr(logins, "_CONFIG_DIR_OVERRIDE", tmp_path / "login-store")


def _save_custom_login(provider_id: str, sdk: str = "openai") -> None:
    logins.save_login(
        Login(
            provider_id=provider_id,
            sdk_provider_id=sdk,
            provider_base_url=BASE_URL,
            provider_api_key="x",
        )
    )


def test_get_provider_synthesizes_def_from_saved_login():
    _save_custom_login("testes")
    provider = providers.get_provider("testes")
    assert provider is not None
    assert provider.id == "testes"
    assert provider.transport == "custom_openai"
    assert provider.effective_sdk_provider_id == "openai"
    assert provider.default_base_url == BASE_URL


def test_anthropic_shape_login_gets_anthropic_transport():
    _save_custom_login("myclaude", sdk="anthropic")
    provider = providers.get_provider("myclaude")
    assert provider is not None
    assert provider.transport == "custom_anthropic"


def test_prefix_splits_on_saved_login():
    _save_custom_login("testes")
    assert providers.parse_model_prefix("testes/test") == ("testes", "test")


def test_prefix_splits_on_saved_login_with_path_model():
    _save_custom_login("booty")
    assert providers.parse_model_prefix(f"booty/{GGUF}") == ("booty", GGUF)


def test_unknown_prefix_still_passes_through_whole():
    assert providers.parse_model_prefix("nope/some-model") == (None, "nope/some-model")


def test_route_by_prefix_carries_login_endpoint():
    _save_custom_login("testes")
    route = resolve_model_route("testes/test", env={}, explicit_model=True)
    assert route.provider_id == "testes"
    assert route.model == "test"
    assert route.base_url == BASE_URL
    assert route.api_key == "x"
    assert route.transport == "custom_openai"


def test_login_prefix_overrides_pinned_provider():
    _save_custom_login("testes")
    route = resolve_model_route(
        "testes/test",
        configured_provider_id="deepseek",
        env={},
        explicit_model=True,
    )
    assert route.provider_id == "testes"
    assert route.model == "test"
    assert route.base_url == BASE_URL


def test_pinned_custom_provider_resolves_transport_and_endpoint():
    _save_custom_login("testes")
    route = resolve_model_route(
        "test",
        configured_provider_id="testes",
        env={},
        explicit_model=True,
    )
    assert route.provider_id == "testes"
    assert route.model == "test"
    assert route.base_url == BASE_URL
    assert route.transport == "custom_openai"


def test_known_provider_ids_includes_saved_logins():
    _save_custom_login("testes")
    assert "testes" in providers.known_provider_ids()


def test_missing_login_store_never_breaks_lookup():
    assert providers.get_provider("testes") is None
