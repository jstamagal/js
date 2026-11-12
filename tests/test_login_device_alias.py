"""Codex device aliases retain their selected auth flow after normalization."""

from types import SimpleNamespace

import pytest

from js import cli, login_cli


@pytest.fixture
def flows(monkeypatch):
    called = []
    login = SimpleNamespace(provider_id="openai-codex")
    def device():
        called.append("device")
        return login
    def browser():
        called.append("browser")
        return login
    monkeypatch.setattr(login_cli.codex_auth, "login_device", device)
    monkeypatch.setattr(login_cli.codex_auth, "login_browser", browser)
    monkeypatch.setattr(login_cli, "test_login", lambda login: [])
    monkeypatch.setattr(login_cli, "_keep_list", lambda *args: (None, login))
    return called


@pytest.mark.parametrize("alias", ["openai-codex-device", "OpenAI-Codex-Device", " OPENAI-CODEX-DEVICE\t"])
@pytest.mark.parametrize("rail", ["codex", "login", "cli"])
def test_device_alias_spelling_uses_device_flow(flows, alias, rail):
    if rail == "codex":
        result = login_cli._run_codex_login(alias)
    elif rail == "login":
        result = login_cli._run_login(alias)
    else:
        result = cli.main(["--login", alias])
    assert result == 0
    assert flows == ["device"]


@pytest.mark.parametrize("alias", ["openai-codex", "OpenAI-Codex", " openai-codex "])
def test_ordinary_codex_alias_still_uses_browser_flow(flows, alias):
    assert login_cli._run_login(alias) == 0
    assert flows == ["browser"]
