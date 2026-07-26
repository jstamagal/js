from __future__ import annotations

import json

from js import codex_models, runtime


def _write_manifest(tmp_path, monkeypatch, models):
    home = tmp_path / "codexhome"
    home.mkdir(exist_ok=True)
    (home / "models_cache.json").write_text(json.dumps({
        "fetched_at": "2026-07-26T00:00:00Z",
        "etag": "abc",
        "client_version": "0.145.0",
        "models": models,
    }), encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(home))
    codex_models._CACHE_STAMP = None
    return home


def test_codex_manifest_supplies_the_subscription_window(tmp_path, monkeypatch):
    # models.dev says gpt-5.6-sol is 1,050,000 (the public API row). The codex
    # subscription serves 272,000 and says so in its own cache.
    _write_manifest(tmp_path, monkeypatch, [
        {"slug": "gpt-5.6-sol", "context_window": 272000,
         "max_context_window": 272000, "effective_context_window_percent": 95},
    ])
    assert codex_models.context_window("gpt-5.6-sol") == 272000
    assert runtime._resolve_context_window("gpt-5.6-sol", "openai-codex", None) == 272000
    # Another provider serving the same id is unaffected.
    assert runtime._resolve_context_window("gpt-5.6-sol", "openrouter", None) != 272000


def test_context_window_prefers_max_context_window_never(tmp_path, monkeypatch):
    # gpt-5.4 ships context_window 272000 against max_context_window 1000000;
    # the smaller one is what this account may actually send.
    _write_manifest(tmp_path, monkeypatch, [
        {"slug": "gpt-5.4", "context_window": 272000, "max_context_window": 1000000},
    ])
    assert codex_models.context_window("gpt-5.4") == 272000


def test_explicit_override_still_beats_the_manifest(tmp_path, monkeypatch):
    _write_manifest(tmp_path, monkeypatch, [
        {"slug": "gpt-5.6-sol", "context_window": 272000},
    ])
    try:
        runtime.set_context_window_overrides({"openai-codex/gpt-5.6-sol": 200_000})
        assert runtime._resolve_context_window("gpt-5.6-sol", "openai-codex", None) == 200_000
    finally:
        runtime.set_context_window_overrides(None)


def test_missing_or_corrupt_manifest_is_not_fatal(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "nope"))
    codex_models._CACHE_STAMP = None
    assert codex_models.context_window("gpt-5.6-sol") is None

    home = tmp_path / "broken"
    home.mkdir()
    (home / "models_cache.json").write_text('{"models": [trunc', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(home))
    codex_models._CACHE_STAMP = None
    assert codex_models.context_window("gpt-5.6-sol") is None


def test_manifest_refresh_is_picked_up_without_restart(tmp_path, monkeypatch):
    home = _write_manifest(tmp_path, monkeypatch, [
        {"slug": "gpt-5.6-sol", "context_window": 272000},
    ])
    assert codex_models.context_window("gpt-5.6-sol") == 272000
    (home / "models_cache.json").write_text(json.dumps({
        "models": [{"slug": "gpt-5.6-sol", "context_window": 400000}],
    }), encoding="utf-8")
    import os
    st = (home / "models_cache.json").stat()
    os.utime(home / "models_cache.json", (st.st_atime + 10, st.st_mtime + 10))
    assert codex_models.context_window("gpt-5.6-sol") == 400000
