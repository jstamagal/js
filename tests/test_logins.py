"""Tests for js.logins multi-provider login store."""

from __future__ import annotations

from pathlib import Path

import pytest

from js import logins, paths

@pytest.fixture
def tmp_logins_dir(tmp_path: Path, monkeypatch):
    logins.set_config_dir(tmp_path)
    yield tmp_path
    logins.set_config_dir(paths.login_store_dir())


def test_load_logins_empty(tmp_logins_dir):
    assert logins.load_logins() == {}


def test_load_logins_degrades_to_empty_on_corrupt_file(tmp_logins_dir):
    # Read paths run on every routing/picker call and must never crash — a
    # malformed file just looks like "no logins" here.
    (tmp_logins_dir / "logins.toml").write_bytes(b"not valid toml {{{")
    assert logins.load_logins() == {}


def test_save_login_refuses_to_overwrite_a_corrupt_file(tmp_logins_dir):
    path = tmp_logins_dir / "logins.toml"
    path.write_bytes(b"not valid toml {{{")
    with pytest.raises(logins.LoginsCorruptError):
        logins.save_login(logins.Login(provider_id="x", provider_api_key="k"))
    # Refusing means refusing: the corrupt bytes are untouched, not truncated
    # down to the one login this call knew about.
    assert path.read_bytes() == b"not valid toml {{{"


def test_remove_login_refuses_to_overwrite_a_corrupt_file(tmp_logins_dir):
    path = tmp_logins_dir / "logins.toml"
    path.write_bytes(b"not valid toml {{{")
    with pytest.raises(logins.LoginsCorruptError):
        logins.remove_login("x")
    assert path.read_bytes() == b"not valid toml {{{"


def test_save_login_writes_atomically_with_no_leftover_temp_file(tmp_logins_dir):
    logins.save_login(logins.Login(provider_id="a", provider_api_key="k"))
    names = {p.name for p in tmp_logins_dir.iterdir()}
    assert names == {"logins.toml"}


def test_save_and_load_login(tmp_logins_dir):
    logins.save_login(logins.Login(provider_id="openai", provider_api_key="sk-test"))
    loaded = logins.load_logins()
    assert "openai" in loaded
    assert loaded["openai"].provider_api_key == "sk-test"



def test_save_login_writes_private_file_mode(tmp_logins_dir):
    logins.save_login(logins.Login(provider_id="openai", provider_api_key="sk-test"))
    mode = logins.login_path().stat().st_mode & 0o777
    assert mode == 0o600

def test_save_login_updates_existing(tmp_logins_dir):
    logins.save_login(logins.Login(provider_id="x", provider_api_key="old"))
    logins.save_login(logins.Login(provider_id="x", provider_api_key="new"))
    assert logins.load_logins()["x"].provider_api_key == "new"



def test_login_effective_provider_id_round_trip(tmp_logins_dir):
    logins.save_login(
        logins.Login(
            provider_id="my-proxy",
            sdk_provider_id="openai",
            provider_base_url="http://proxy.test/v1",
            provider_api_key="sk-proxy",
        )
    )
    loaded = logins.load_logins()["my-proxy"]
    assert loaded.effective_provider_id == "openai"
    assert loaded.provider_base_url == "http://proxy.test/v1"


def test_test_login_uses_effective_provider_id(monkeypatch, tmp_logins_dir):
    captured = {}

    class Provider:
        async def list_models(self):
            return ["model-a"]

        async def aclose(self):
            captured["closed"] = True

    def fake_get_provider(provider_id, **kwargs):
        captured["provider_id"] = provider_id
        captured.update(kwargs)
        return Provider()

    monkeypatch.setattr(logins.ai, "get_provider", fake_get_provider)
    models = logins.test_login(
        logins.Login(
            provider_id="my-proxy",
            sdk_provider_id="openai",
            provider_base_url="http://proxy.test/v1",
            provider_api_key="sk-proxy",
        )
    )

    assert models == ["model-a"]
    assert captured == {
        "provider_id": "openai",
        "base_url": "http://proxy.test/v1",
        "api_key": "sk-proxy",
        "headers": None,
        "closed": True,
    }

def test_remove_login(tmp_logins_dir):
    logins.save_login(logins.Login(provider_id="x", provider_api_key="k"))
    assert logins.remove_login("x") is True
    assert logins.load_logins() == {}
    assert logins.remove_login("x") is False


def test_model_cache_round_trip(tmp_logins_dir):
    logins.cache_models("openai", ["gpt-4o", "gpt-4.1"])
    cache = logins.load_model_cache()
    assert cache["openai"] == ["gpt-4o", "gpt-4.1"]


def test_remove_login_clears_cache(tmp_logins_dir):
    logins.save_login(logins.Login(provider_id="x", provider_api_key="k"))
    logins.cache_models("x", ["a", "b"])
    logins.remove_login("x")
    assert "x" not in logins.load_model_cache()


def test_login_path_creates_dir(tmp_path: Path, monkeypatch):
    new_dir = tmp_path / "fresh"
    logins.set_config_dir(new_dir)
    path = logins.login_path()
    assert path.parent.exists()
    logins.set_config_dir(Path.home() / ".config" / "js")
