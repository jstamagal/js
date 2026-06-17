from __future__ import annotations

import asyncio
from pathlib import Path

from js import logins, paths, picker, providers


def _reset_logins() -> None:
    logins.set_config_dir(paths.login_store_dir())


def test_model_picker_opens_without_logins(tmp_path: Path):
    logins.set_config_dir(tmp_path)

    async def smoke() -> None:
        app = picker.ModelPicker()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert len(app.query_one("#provider-list").children) == 0
            assert len(app.query_one("#model-list").children) == 1
            assert getattr(app.query_one("#detail"), "_Static__content") == "no logged-in providers — use /login or js --login"

    try:
        asyncio.run(smoke())
    finally:
        _reset_logins()


def test_model_picker_shows_saved_login_models(tmp_path: Path):
    logins.set_config_dir(tmp_path)
    logins.save_login(logins.Login(provider_id="deepseek", provider_api_key="sk-test"))
    logins.cache_models("deepseek", ["deepseek-v4-flash"])

    async def smoke() -> None:
        app = picker.ModelPicker(provider_id="deepseek", model="deepseek-v4-flash")
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.query_one("#provider-list").index == 0
            assert app.query_one("#model-list").index == 0
            assert app._model_rows[0].id == "deepseek-v4-flash"

    try:
        asyncio.run(smoke())
    finally:
        _reset_logins()


def test_provider_rows_include_saved_logins_only(tmp_path: Path):
    logins.set_config_dir(tmp_path)
    logins.save_login(logins.Login(provider_id="deepseek", provider_api_key="sk-test"))

    try:
        rows = {row.id: row for row in picker._provider_rows()}
        assert rows["deepseek"].source == "login"
        assert "ollama" not in rows
        assert "openai" not in rows
    finally:
        _reset_logins()


def test_model_rows_use_cache_before_provider_default(tmp_path: Path):
    logins.set_config_dir(tmp_path)
    logins.cache_models("deepseek", ["cached-model"])
    try:
        rows = picker._model_rows("deepseek")
        assert [row.id for row in rows] == ["cached-model"]
    finally:
        _reset_logins()


def test_model_rows_require_cached_models(tmp_path: Path):
    logins.set_config_dir(tmp_path)
    try:
        rows = picker._model_rows("deepseek")
        assert rows == []
    finally:
        _reset_logins()


def test_picker_fetch_action_updates_model_cache(monkeypatch, tmp_path: Path):
    logins.set_config_dir(tmp_path)
    logins.save_login(logins.Login(provider_id="deepseek", provider_api_key="sk-test"))

    async def fake_fetch(_login):
        return ["fresh-model"]

    monkeypatch.setattr(logins, "fetch_models", fake_fetch)

    async def smoke() -> None:
        app = picker.ModelPicker(provider_id="deepseek")
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("f")
            await pilot.pause()
            assert logins.load_model_cache()["deepseek"] == ["fresh-model"]
            assert app._model_rows[0].id == "fresh-model"

    try:
        asyncio.run(smoke())
    finally:
        _reset_logins()


def test_picker_enter_selects_model(tmp_path: Path):
    logins.set_config_dir(tmp_path)
    logins.save_login(logins.Login(provider_id="deepseek", provider_api_key="sk-test"))
    logins.cache_models("deepseek", ["deepseek-v4-flash"])

    async def smoke() -> None:
        app = picker.ModelPicker(provider_id="deepseek", model="deepseek-v4-flash")
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("tab")
            await pilot.press("enter")
            await pilot.pause()
            assert app.return_value == {
                "provider_id": "deepseek",
                "provider_base_url": None,
                "provider_api_key": "sk-test",
                "provider_headers": {},
                "model": "deepseek-v4-flash",
            }

    try:
        asyncio.run(smoke())
    finally:
        _reset_logins()


def test_picker_switching_provider_does_not_leak_prior_base_or_key(tmp_path: Path):
    logins.set_config_dir(tmp_path)
    logins.save_login(logins.Login(provider_id="deepseek", provider_base_url="https://api.deepseek.com", provider_api_key="sk-deepseek"))
    logins.save_login(logins.Login(provider_id="ollama", provider_base_url="http://ollama.test/v1", provider_api_key="ollama"))
    logins.cache_models("deepseek", ["deepseek-v4-flash"])
    logins.cache_models("ollama", ["gemma4:e2b"])

    async def smoke() -> None:
        app = picker.ModelPicker(
            provider_id="deepseek",
            provider_base_url="https://api.deepseek.com",
            provider_api_key="sk-deepseek",
            model="deepseek-v4-flash",
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("down")
            await pilot.press("tab")
            await pilot.press("enter")
            await pilot.pause()
            assert app.return_value == {
                "provider_id": "ollama",
                "provider_base_url": "http://ollama.test/v1",
                "provider_api_key": "ollama",
                "provider_headers": {},
                "model": "gemma4:e2b",
            }

    try:
        asyncio.run(smoke())
    finally:
        _reset_logins()


def test_provider_registry_shapes_for_picker_shortcuts():
    assert providers.provider_for_login("ollama").effective_sdk_provider_id == "openai"
    assert providers.provider_for_login("llama.cpp").default_base_url == "http://127.0.0.1:8080/v1"
    assert providers.provider_for_login("mimo-token-plan").append_only is True
    assert providers.provider_for_login("deepseek").reasoning_effort == "xhigh"
