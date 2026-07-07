"""Custom `js --login` providers must be first-class routing citizens.

Regression for the FIXME cluster where a custom login (booty/testes/a local
llama.cpp box) was visible to /model and --list-models but invisible to
routing: `provider/model` prefixes shipped the WHOLE string to the default
provider, and pinning `provider.id` resolved base_url but handed the raw js id
to the SDK ("unknown provider id").
"""

from __future__ import annotations

import pytest

from js import cli, logins, providers
from js.config import from_env
from js.logins import Login
from js.routing import ProviderNotLoggedInError, resolve_model_route

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


def _isolate_config(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    home = tmp_path / "home"
    config_home = home / ".config"
    data_home = home / ".local" / "share"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    for name in ("JS_MODEL", "JS_PROVIDER", "JS_BASE_URL", "JS_API_KEY"):
        monkeypatch.delenv(name, raising=False)


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


def test_explicit_model_known_unlogged_prefix_does_not_ride_saved_default(
    monkeypatch, tmp_path
):
    _isolate_config(monkeypatch, tmp_path)
    _save_custom_login("testes")
    config_file = tmp_path / "home" / ".config" / "js" / "jsrc"
    config_file.parent.mkdir(parents=True)
    config_file.write_text("set model.id testes/test\n", encoding="utf-8")
    monkeypatch.setenv("HF_TOKEN", "hf-decoy")

    cfg = from_env(cwd=tmp_path, save_session=False)
    assert cfg.provider_id == "testes"
    assert cfg.model == "test"

    with pytest.raises(ProviderNotLoggedInError) as excinfo:
        cli._resolve_cli_model_override(cfg, "huggingface/foo")

    assert "provider 'huggingface' is not logged in" in str(excinfo.value)


def test_js_model_known_unlogged_prefix_does_not_ride_saved_default(monkeypatch, tmp_path):
    _isolate_config(monkeypatch, tmp_path)
    _save_custom_login("testes")
    config_file = tmp_path / "home" / ".config" / "js" / "jsrc"
    config_file.parent.mkdir(parents=True)
    config_file.write_text("set model.id testes/test\n", encoding="utf-8")
    monkeypatch.setenv("JS_MODEL", "huggingface/foo")
    monkeypatch.setenv("HF_TOKEN", "hf-decoy")

    with pytest.raises(ProviderNotLoggedInError) as excinfo:
        from_env(cwd=tmp_path, save_session=False)

    assert "provider 'huggingface' is not logged in" in str(excinfo.value)


def test_explicit_gateway_provider_keeps_known_provider_model_id(monkeypatch, tmp_path):
    _isolate_config(monkeypatch, tmp_path)
    monkeypatch.setenv("JS_PROVIDER", "omp")
    monkeypatch.setenv("JS_BASE_URL", "https://omp.test/v1")
    monkeypatch.setenv("JS_MODEL", "anthropic/claude-x")

    cfg = from_env(cwd=tmp_path, save_session=False)

    assert cfg.provider_id == "omp"
    assert cfg.model == "anthropic/claude-x"
    assert cfg.provider_base_url == "https://omp.test/v1"
    assert cfg.explicit_provider is True


def test_cli_model_override_keeps_known_provider_id_under_explicit_gateway(monkeypatch, tmp_path):
    _isolate_config(monkeypatch, tmp_path)
    monkeypatch.setenv("JS_PROVIDER", "omp")
    monkeypatch.setenv("JS_BASE_URL", "https://omp.test/v1")

    cfg = from_env(cwd=tmp_path, save_session=False)
    actual = cli._resolve_cli_model_override(cfg, "anthropic/claude-x")

    assert actual.provider_id == "omp"
    assert actual.model == "anthropic/claude-x"
    assert actual.provider_base_url == "https://omp.test/v1"


def test_explicit_gateway_provider_still_yields_to_saved_login_prefix(monkeypatch, tmp_path):
    _isolate_config(monkeypatch, tmp_path)
    _save_custom_login("testes")
    monkeypatch.setenv("JS_PROVIDER", "omp")
    monkeypatch.setenv("JS_BASE_URL", "https://omp.test/v1")
    monkeypatch.setenv("JS_MODEL", "testes/test")

    cfg = from_env(cwd=tmp_path, save_session=False)

    assert cfg.provider_id == "testes"
    assert cfg.model == "test"
    assert cfg.provider_base_url == BASE_URL
    assert cfg.provider_api_key == "x"


def test_explicit_model_same_saved_prefix_routes_to_saved_login(monkeypatch, tmp_path):
    _isolate_config(monkeypatch, tmp_path)
    _save_custom_login("testes")
    config_file = tmp_path / "home" / ".config" / "js" / "jsrc"
    config_file.parent.mkdir(parents=True)
    config_file.write_text("set model.id testes/test\n", encoding="utf-8")

    cfg = from_env(cwd=tmp_path, save_session=False)
    actual = cli._resolve_cli_model_override(cfg, "testes/other")

    assert actual.provider_id == "testes"
    assert actual.model == "other"
    assert actual.provider_base_url == BASE_URL
    assert actual.provider_api_key == "x"


def test_pinned_gateway_unknown_prefix_passes_through_whole_model_id():
    route = resolve_model_route(
        "unknown-vendor/model-name",
        configured_provider_id="omp",
        configured_base_url="https://gateway.test/v1",
        configured_api_key="sk-omp",
        env={},
        explicit_model=True,
        prefix_overrides_provider=True,
    )

    assert route.provider_id == "omp"
    assert route.model == "unknown-vendor/model-name"
    assert route.base_url == "https://gateway.test/v1"
    assert route.api_key == "sk-omp"
