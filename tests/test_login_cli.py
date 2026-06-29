from __future__ import annotations

from pathlib import Path

from js import cli, login_cli, logins, paths, providers


def _reset_logins() -> None:
    logins.set_config_dir(paths.login_store_dir())


def test_login_cli_main_accepts_provider_argument(monkeypatch):
    captured = {}

    def fake_run_login(provider_id=None):
        captured["provider_id"] = provider_id
        return 0

    monkeypatch.setattr(login_cli, "_run_login", fake_run_login)
    assert login_cli.main(["deepseek"]) == 0
    assert captured == {"provider_id": "deepseek"}


def test_login_cli_main_accepts_login_subcommand(monkeypatch):
    captured = {}

    def fake_run_login(provider_id=None):
        captured["provider_id"] = provider_id
        return 0

    monkeypatch.setattr(login_cli, "_run_login", fake_run_login)
    assert login_cli.main(["--login", "openai-completions"]) == 0
    assert captured == {"provider_id": "openai-completions"}


def test_login_cli_provider_list_includes_first_class_shortcuts():
    rows = {provider.id: provider.display_name for provider in providers.all_providers()}

    assert rows["ollama"].startswith("Ollama")
    assert rows["llama.cpp"].startswith("llama.cpp")
    assert rows["mimo"].startswith("Xiaomi MiMo")
    assert rows["mimo-token-plan"].startswith("Xiaomi MiMo Token Plan")


def test_login_registry_includes_modelsdotdev_provider(tmp_path: Path):
    logins.set_config_dir(tmp_path)
    try:
        rows = {provider.id: provider.display_name for provider in providers.login_providers()}
        assert rows["alibaba"] == "Alibaba"
        assert rows["openai-codex"].startswith("OpenAI Codex OAuth")
    finally:
        _reset_logins()


def test_login_provider_rows_show_saved_env_and_registry(monkeypatch, tmp_path: Path):
    logins.set_config_dir(tmp_path)
    logins.save_login(logins.Login(provider_id="deepseek", provider_api_key="sk-test"))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    try:
        rows = {provider_id: (name, source) for provider_id, name, source in login_cli._login_provider_rows()}
        assert rows["deepseek"][1] == "saved"
        assert rows["openai"][1] == "env"
        assert rows["alibaba"][1] == "registry"
    finally:
        _reset_logins()


def test_collect_api_login_uses_env_key_without_prompt(monkeypatch, tmp_path: Path):
    logins.set_config_dir(tmp_path)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env-deepseek")
    monkeypatch.setattr(login_cli, "_input", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("prompted unexpectedly")))
    try:
        login = login_cli._collect_api_login("deepseek", "deepseek", providers.provider_for_login("deepseek"))
        assert login is not None
        assert login.provider_api_key == "sk-env-deepseek"
        assert login.provider_base_url == "https://api.deepseek.com"
    finally:
        _reset_logins()


def test_opencode_go_anthropic_uses_anthropic_root_base_url():
    provider = providers.provider_for_login("opencode-go-anthropic")
    assert provider.default_base_url == "https://opencode.ai/zen/go"
    assert provider.base_url_env == ("OPENCODE_GO_ANTHROPIC_BASE_URL",)
    assert provider.models_list_validates_auth is False


def test_opencode_go_login_filters_openai_models():
    provider = providers.provider_for_login("opencode-go")
    models = provider.filter_models(
        [
            "deepseek-v4-flash",
            "glm-5.1",
            "minimax-m3",
            "qwen3.7-plus",
            "mimo-v2.5-pro",
        ]
    )
    assert models == ["deepseek-v4-flash", "glm-5.1", "mimo-v2.5-pro"]


def test_opencode_go_anthropic_login_filters_anthropic_models():
    provider = providers.provider_for_login("opencode-go-anthropic")
    models = provider.filter_models(
        [
            "deepseek-v4-flash",
            "glm-5.1",
            "minimax-m3",
            "qwen3.7-plus",
            "mimo-v2.5-pro",
        ]
    )
    assert models == ["minimax-m3", "qwen3.7-plus"]


def test_secondary_test_enter_adds_without_testing(monkeypatch):
    # Empty input = add without a test, in BOTH modes (require_test only warns).
    monkeypatch.setattr("builtins.input", lambda _prompt="": "")
    assert login_cli._secondary_test_choice(["first", "second"], require_test=True) is True
    assert login_cli._secondary_test_choice(["first", "second"], require_test=False) is True


def test_secondary_test_number_picks_that_model(monkeypatch):
    # A model number means "verify that one"; it returns the model id to test.
    monkeypatch.setattr("builtins.input", lambda _prompt="": "2")
    assert login_cli._secondary_test_choice(["first", "second"], require_test=True) == "second"


def test_login_cli_logout_requires_provider(capsys):
    assert login_cli.main(["logout"]) == 2
    assert "--logout <provider-id>" in capsys.readouterr().err


def test_login_cli_logout_dispatch(monkeypatch):
    captured = {}

    def fake_run_logout(provider_id):
        captured["provider_id"] = provider_id
        return 0

    monkeypatch.setattr(login_cli, "_run_logout", fake_run_logout)
    assert login_cli.main(["logout", "deepseek"]) == 0
    assert captured == {"provider_id": "deepseek"}


def test_top_level_cli_dispatches_login(monkeypatch):
    captured = {}

    def fake_login_main(args):
        captured["args"] = args
        return 0

    monkeypatch.setattr(login_cli, "main", fake_login_main)
    monkeypatch.setattr(cli, "login_cli", login_cli, raising=False)
    assert cli.main(["--login", "deepseek"]) == 0
    assert captured == {"args": ["deepseek"]}
