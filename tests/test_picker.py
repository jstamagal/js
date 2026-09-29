from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from js import logins, paths, picker, providers


def _reset_logins() -> None:
    logins.set_config_dir(paths.login_store_dir())


async def _drive(state: picker.ModelPicker, *keys: str):
    """Run the picker app, typing each key in turn; return the app's result."""
    with create_pipe_input() as pipe:
        app = state.application(input=pipe, output=DummyOutput())
        task = asyncio.ensure_future(app.run_async())
        for key in keys:
            await asyncio.sleep(0.05)
            pipe.send_text(key)
        return await asyncio.wait_for(task, timeout=10)


def test_model_picker_opens_without_logins(tmp_path: Path):
    logins.set_config_dir(tmp_path)

    async def smoke() -> None:
        state = picker.ModelPicker()
        assert state.provider_rows == []
        assert await _drive(state, "\r", "q") is None

    try:
        asyncio.run(smoke())
    finally:
        _reset_logins()


@pytest.mark.parametrize("cancel_key", ["q", "\x1b", "\x03"])
def test_model_picker_shows_saved_login_models(tmp_path: Path, cancel_key: str):
    logins.set_config_dir(tmp_path)
    logins.save_login(logins.Login(provider_id="deepseek", provider_api_key="sk-test"))
    logins.cache_models("deepseek", ["deepseek-v4-flash"])

    async def smoke() -> None:
        state = picker.ModelPicker(provider_id="deepseek", model="deepseek-v4-flash")
        assert state.provider_index == 0
        assert state.model_index == 0
        assert state.model_rows[0].id == "deepseek-v4-flash"
        # With a model selectable, each cancel key closes the picker with no choice.
        assert await _drive(state, cancel_key) is None

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


def test_codex_model_rows_are_exactly_the_cache(tmp_path: Path):
    # the picker used to splice in a hardcoded model id to paper over a stale
    # listing; the listing is fixed at the source now, so it shows the cache
    logins.set_config_dir(tmp_path)
    logins.cache_models("openai-codex", ["codex-auto-review", "gpt-5.4", "gpt-5.6-sol"])
    try:
        rows = picker._model_rows("openai-codex")
        assert [row.id for row in rows] == ["codex-auto-review", "gpt-5.4", "gpt-5.6-sol"]
    finally:
        _reset_logins()


def test_picker_fetch_action_updates_model_cache(monkeypatch, tmp_path: Path):
    logins.set_config_dir(tmp_path)
    logins.save_login(logins.Login(provider_id="deepseek", provider_api_key="sk-test"))

    async def fake_fetch(_login):
        return ["fresh-model"]

    monkeypatch.setattr(logins, "fetch_models", fake_fetch)

    async def smoke() -> None:
        state = picker.ModelPicker(provider_id="deepseek")
        assert await _drive(state, "f", "q") is None
        assert logins.load_model_cache()["deepseek"] == ["fresh-model"]
        assert state.model_rows[0].id == "fresh-model"

    try:
        asyncio.run(smoke())
    finally:
        _reset_logins()


def test_picker_enter_selects_model(tmp_path: Path):
    logins.set_config_dir(tmp_path)
    logins.save_login(logins.Login(provider_id="deepseek", provider_api_key="sk-test"))
    logins.cache_models("deepseek", ["deepseek-v4-flash"])

    async def smoke() -> None:
        state = picker.ModelPicker(provider_id="deepseek", model="deepseek-v4-flash")
        assert await _drive(state, "\t", "\r") == {
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
        state = picker.ModelPicker(
            provider_id="deepseek",
            provider_base_url="https://api.deepseek.com",
            provider_api_key="sk-deepseek",
            model="deepseek-v4-flash",
        )
        # down arrow moves to the next provider, tab to the model pane.
        assert await _drive(state, "\x1b[B", "\t", "\r") == {
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
