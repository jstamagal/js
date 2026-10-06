"""The provider table and the login store are read once per version of their
files, not once per routing lookup."""

from __future__ import annotations

import os
import time

import pytest

from js import logins, model_metadata, paths, providers


@pytest.fixture
def tmp_logins_dir(tmp_path):
    logins.set_config_dir(tmp_path)
    yield tmp_path
    logins.set_config_dir(paths.login_store_dir())


def _count_calls(monkeypatch, module, name):
    calls = []
    original = getattr(module, name)

    def counted(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, name, counted)
    return calls


def test_catalog_is_read_once_while_unchanged(monkeypatch):
    monkeypatch.setattr(providers, "_dynamic", None)
    reads = _count_calls(monkeypatch, providers.modelsdotdev, "iter_providers")
    first = providers._dynamic_login_providers()
    assert providers._dynamic_login_providers() is first
    assert providers.normalize_provider_id("groq") == "groq"
    assert len(reads) == 1


def test_catalog_is_read_again_when_its_files_change(monkeypatch):
    monkeypatch.setattr(providers, "_dynamic", None)
    reads = _count_calls(monkeypatch, providers.modelsdotdev, "iter_providers")
    providers._dynamic_login_providers()
    status = model_metadata._status_file_path()
    stamp = os.stat(status)
    os.utime(status, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1_000_000))
    providers._dynamic_login_providers()
    assert len(reads) == 2


def test_catalog_is_rechecked_after_the_recheck_interval(monkeypatch):
    monkeypatch.setattr(providers, "_dynamic", None)
    reads = _count_calls(monkeypatch, providers.modelsdotdev, "iter_providers")
    providers._dynamic_login_providers()
    key, checked_at, rows = providers._dynamic
    monkeypatch.setattr(providers, "_dynamic", (key, checked_at - providers._CATALOG_RECHECK_S, rows))
    providers._dynamic_login_providers()
    assert len(reads) == 2


def test_logins_are_parsed_once_while_the_file_is_unchanged(tmp_logins_dir, monkeypatch):
    logins.save_login(logins.Login(provider_id="mine", provider_api_key="k1"))
    parses = _count_calls(monkeypatch, logins.tomllib, "load")
    assert logins.load_logins()["mine"].provider_api_key == "k1"
    assert logins.load_logins()["mine"].provider_api_key == "k1"
    assert len(parses) == 1


def test_logins_reload_after_a_write(tmp_logins_dir):
    logins.save_login(logins.Login(provider_id="mine", provider_api_key="k1"))
    assert logins.load_logins()["mine"].provider_api_key == "k1"
    logins.save_login(logins.Login(provider_id="mine", provider_api_key="k2"))
    assert logins.load_logins()["mine"].provider_api_key == "k2"
    logins.remove_login("mine")
    assert logins.load_logins() == {}


def test_logins_reload_after_the_file_changes_underneath(tmp_logins_dir):
    logins.save_login(logins.Login(provider_id="mine", provider_api_key="k1"))
    assert logins.load_logins()["mine"].provider_api_key == "k1"
    path = tmp_logins_dir / "logins.toml"
    # Another process rewriting the file: new inode via rename, as js does.
    tmp = path.with_suffix(".new")
    tmp.write_bytes(path.read_bytes().replace(b"k1", b"k2"))
    os.replace(tmp, path)
    assert logins.load_logins()["mine"].provider_api_key == "k2"


def test_a_caller_cannot_change_what_the_next_load_returns(tmp_logins_dir):
    logins.save_login(logins.Login(provider_id="mine", provider_api_key="k1"))
    loaded = logins.load_logins()
    loaded.pop("mine")
    assert "mine" in logins.load_logins()


def test_stat_only_lookups_are_cheap(tmp_logins_dir, monkeypatch):
    """The point of the cache: a routing lookup after the first costs stats,
    not a catalog read. A loose bound, far below one catalog parse."""
    logins.save_login(logins.Login(provider_id="mine", provider_api_key="k1"))
    providers.normalize_provider_id("mine")
    logins.load_logins()
    started = time.perf_counter()
    for _ in range(200):
        providers.normalize_provider_id("mine")
        logins.load_logins()
    assert time.perf_counter() - started < 0.5
