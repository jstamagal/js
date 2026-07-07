from __future__ import annotations

import sys
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


def test_collect_api_login_offers_env_key_and_accepting_uses_it(monkeypatch, tmp_path: Path):
    # Owner ruling: login never silently farms env keys. It OFFERS the env key;
    # answering yes uses it, and only then.
    logins.set_config_dir(tmp_path)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env-deepseek")
    prompts: list[str] = []

    def scripted_input(prompt, *, default=None, secret=False):
        prompts.append(prompt)
        if "use it?" in prompt:
            return "y"
        raise AssertionError(f"unexpected prompt: {prompt}")

    monkeypatch.setattr(login_cli, "_input", scripted_input)
    try:
        login = login_cli._collect_api_login("deepseek", "deepseek", providers.provider_for_login("deepseek"))
        assert login is not None
        assert login.provider_api_key == "sk-env-deepseek"
        assert login.provider_base_url == "https://api.deepseek.com"
        assert any("ENV:DEEPSEEK_API_KEY" in p for p in prompts)
    finally:
        _reset_logins()


def test_collect_api_login_declining_env_key_prompts_for_one(monkeypatch, tmp_path: Path):
    logins.set_config_dir(tmp_path)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env-decoy")

    def scripted_input(prompt, *, default=None, secret=False):
        if "use it?" in prompt:
            return "n"
        if prompt.startswith("Enter API Key"):
            return "sk-typed-real"
        raise AssertionError(f"unexpected prompt: {prompt}")

    monkeypatch.setattr(login_cli, "_input", scripted_input)
    try:
        login = login_cli._collect_api_login("deepseek", "deepseek", providers.provider_for_login("deepseek"))
        assert login is not None
        assert login.provider_api_key == "sk-typed-real"
    finally:
        _reset_logins()


def test_local_providers_always_show_base_url_prompt():
    # Finding 58: llama.cpp/ollama/cliproxyapi ship a default 127.0.0.1-style
    # endpoint that "established"/no-established-flag logic treated as fixed,
    # so the login prompt was skipped and a fresh box silently aimed at a
    # port nothing was listening on. The endpoint differs per box, so the
    # prompt must never be skippable for these — the default only seeds it.
    for provider_id in ("ollama", "llama.cpp", "cliproxyapi"):
        assert providers.provider_for_login(provider_id).login_base_url_field is True

    # A genuinely fixed remote endpoint is unaffected and still skips the prompt.
    assert providers.provider_for_login("deepseek").login_base_url_field is False
    assert providers.provider_for_login("ollama-cloud").login_base_url_field is False


def test_variable_endpoint_providers_show_base_url_field():
    for provider_id in (
        "llama.cpp",
        "ollama",
        "vllm",
        "lmstudio",
        "koboldcpp",
        "text-generation-webui",
        "localai",
        "tabbyapi",
        "tgi",
        "sglang",
        "llamafile",
        "jan",
        "xinference",
    ):
        provider = providers.provider_for_login(provider_id)
        assert provider.variable_endpoint is True
        assert provider.login_base_url_field is True


def test_collect_api_login_prompts_for_base_url_on_local_providers(tmp_path: Path, monkeypatch):
    logins.set_config_dir(tmp_path)
    # This box has real LLAMACPP_* env vars set (the owner's actual llama.cpp
    # box) — clear them so the test sees the registry's own hardcoded default.
    for name in ("LLAMACPP_BASE_URL", "LLAMA_CPP_BASE_URL", "LLAMACPP_API_KEY", "LLAMA_CPP_API_KEY", "LLAMACPP_MODEL", "LLAMA_CPP_MODEL"):
        monkeypatch.delenv(name, raising=False)
    prompted: dict[str, str | None] = {}

    def fake_input(prompt, *, default=None, secret=False):
        prompted[prompt] = default
        return default

    monkeypatch.setattr(login_cli, "_input", fake_input)
    try:
        login = login_cli._collect_api_login("llama.cpp", "openai", providers.provider_for_login("llama.cpp"))
        assert login is not None
        # The hardcoded default only seeds the prompt — it never skips it.
        assert prompted == {"Base URL": "http://127.0.0.1:8080/v1"}
        assert login.provider_base_url == "http://127.0.0.1:8080/v1"
    finally:
        _reset_logins()


def test_opencode_go_anthropic_uses_anthropic_root_base_url():
    provider = providers.provider_for_login("opencode-go-anthropic")
    assert provider.default_base_url == "https://opencode.ai/zen/go"
    assert provider.base_url_env == ("OPENCODE_GO_ANTHROPIC_BASE_URL",)
    assert provider.models_list_validates_auth is False


def test_fetch_models_passes_through_live_list_without_allowlist(monkeypatch):
    # No client-side allowlist: whatever the endpoint serves is what the login
    # picker shows — including freshly shipped ids the old tuple would have hidden.
    import ai

    live = ["glm-5.2", "glm-5.1", "kimi-k2.7-code", "mimo-v2.5", "freshly-shipped"]

    class _FakeProvider:
        async def list_models(self):
            return live

        async def aclose(self):
            return None

    monkeypatch.setattr(ai, "get_provider", lambda *a, **k: _FakeProvider())
    login = logins.Login(
        provider_id="opencode-go",
        sdk_provider_id="openai",
        provider_base_url="https://opencode.ai/zen/go/v1",
        provider_api_key="k",
    )
    assert logins.test_login(login) == live  # nothing filtered out


class _FakeStdscr:
    """Headless curses surface: renders nowhere, feeds a queued key sequence."""

    def __init__(self, keys):
        self._keys = list(keys)

    def keypad(self, _flag):
        pass

    def clear(self):
        pass

    def getmaxyx(self):
        return (24, 80)

    def addstr(self, *_args):
        pass

    def refresh(self):
        pass

    def getch(self):
        return self._keys.pop(0)


def _run_multiselect(monkeypatch, keys, rows, preselected):
    import curses

    monkeypatch.setattr(curses, "curs_set", lambda _n: None)
    return login_cli._curses_multiselect(
        _FakeStdscr(keys), rows, "pick", preselected=set(preselected)
    )


def test_multiselect_spacebar_deselects(monkeypatch):
    import curses

    rows = [("glm-5.2", "openai"), ("glm-5", "openai"), ("kimi-k2.6", "openai")]
    # all preselected; toggle off row0, move down, toggle off row1, confirm -> [2]
    keys = [ord(" "), curses.KEY_DOWN, ord(" "), ord("\n")]
    assert _run_multiselect(monkeypatch, keys, rows, {0, 1, 2}) == [2]


def test_multiselect_none_then_pick(monkeypatch):
    rows = [("a", ""), ("b", ""), ("c", "")]
    keys = [ord("n"), ord(" "), ord("\n")]  # clear all, select row0, confirm
    assert _run_multiselect(monkeypatch, keys, rows, {0, 1, 2}) == [0]


def test_multiselect_cancel_returns_none(monkeypatch):
    rows = [("a", "")]
    assert _run_multiselect(monkeypatch, keys=[ord("q")], rows=rows, preselected={0}) is None


def test_select_models_non_tty_keeps_all(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    models = ["glm-5.2", "glm-5.1", "kimi-k2.7-code"]
    assert login_cli._select_models_to_cache("opencode-go", models) == models


def test_select_models_interactive_plain_enter_keeps_single_fetched_model(monkeypatch, tmp_path: Path):
    import curses

    logins.set_config_dir(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(curses, "curs_set", lambda _n: None)
    keys = [ord("\n")]  # confirm immediately, default selection keeps the model

    def fake_wrapper(fn, *args, **kwargs):
        return fn(_FakeStdscr(keys), *args, **kwargs)

    try:
        monkeypatch.setattr(curses, "wrapper", fake_wrapper)
        monkeypatch.setattr(login_cli, "_input", lambda *a, **k: "")
        assert login_cli._select_models_to_cache("localboy", ["owner-model"]) == ["owner-model"]
    finally:
        _reset_logins()


def test_relogin_plain_enter_drops_cached_models_not_refetched(monkeypatch, tmp_path: Path):
    import curses

    logins.set_config_dir(tmp_path)
    logins.cache_models("openai-codex", ["old-model"])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(curses, "curs_set", lambda _n: None)
    keys = [ord("\n")]
    captured: dict[str, object] = {}

    def fake_wrapper(fn, *args, **kwargs):
        rows = args[0]
        captured["rows"] = [row[0] for row in rows]
        captured["preselected"] = set(kwargs["preselected"])
        return fn(_FakeStdscr(keys), *args, **kwargs)

    login = logins.Login(
        provider_id="openai-codex",
        sdk_provider_id="openai-codex",
        provider_api_key="jwt",
        codex_refresh_token="refresh",
        codex_token_expires=1.0,
        codex_account_id="acct",
        codex_email="owner@example.com",
    )

    try:
        monkeypatch.setattr(curses, "wrapper", fake_wrapper)
        monkeypatch.setattr(login_cli.codex_auth, "login_browser", lambda: login)
        monkeypatch.setattr(login_cli, "test_login", lambda _login: ["new-model"])
        monkeypatch.setattr(login_cli, "_dialect_map", lambda _provider_id: {})
        monkeypatch.setattr(login_cli, "_input", lambda *a, **k: "")

        assert login_cli._run_codex_login("openai-codex") == 0
        assert logins.load_model_cache()["openai-codex"] == ["new-model"]
        assert captured == {"rows": ["new-model", "old-model"], "preselected": {0}}
    finally:
        _reset_logins()


def test_select_models_interactive_with_custom_add(monkeypatch, tmp_path: Path):
    import curses

    logins.set_config_dir(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(curses, "curs_set", lambda _n: None)
    # All preselected: deselect row0 and row2.
    keys = [ord(" "), curses.KEY_DOWN, curses.KEY_DOWN, ord(" "), ord("\n")]

    def fake_wrapper(fn, *args, **kwargs):
        return fn(_FakeStdscr(keys), *args, **kwargs)

    try:
        monkeypatch.setattr(curses, "wrapper", fake_wrapper)
        monkeypatch.setattr(login_cli, "_input", lambda *a, **k: "extra-model, glm-5.2")
        models = ["glm-5.2", "glm-5", "kimi-k2.7-code"]
        # row1 selected; typed ids are explicit additions, even if one appeared in the fetched list.
        assert login_cli._select_models_to_cache("opencode-go", models) == [
            "glm-5", "extra-model", "glm-5.2",
        ]
    finally:
        _reset_logins()


def test_select_models_typed_extras_dedupe_against_selected_fetched(monkeypatch, tmp_path: Path):
    import curses

    logins.set_config_dir(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(curses, "curs_set", lambda _n: None)
    keys = [ord("\n")]

    def fake_wrapper(fn, *args, **kwargs):
        return fn(_FakeStdscr(keys), *args, **kwargs)

    try:
        monkeypatch.setattr(curses, "wrapper", fake_wrapper)
        monkeypatch.setattr(login_cli, "_input", lambda *a, **k: "gpt-5.5,gpt-5.4,gpt-5.4-mini")
        assert login_cli._select_models_to_cache("openai-codex", ["gpt-5.5"]) == [
            "gpt-5.5", "gpt-5.4", "gpt-5.4-mini",
        ]
    finally:
        _reset_logins()


def test_codex_login_typed_extras_survive_cache_and_list_models(
    monkeypatch, tmp_path: Path, capsys,
):
    import curses

    logins.set_config_dir(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(curses, "curs_set", lambda _n: None)
    keys = [ord("\n")]

    def fake_wrapper(fn, *args, **kwargs):
        return fn(_FakeStdscr(keys), *args, **kwargs)

    login = logins.Login(
        provider_id="openai-codex",
        sdk_provider_id="openai-codex",
        provider_api_key="jwt",
        codex_refresh_token="refresh",
        codex_token_expires=1.0,
        codex_account_id="acct",
        codex_email="owner@example.com",
    )
    typed = "gpt-5.4,gpt-5.3-codex-spark,gpt-5.4-mini"

    try:
        monkeypatch.setattr(curses, "wrapper", fake_wrapper)
        monkeypatch.setattr(login_cli.codex_auth, "login_browser", lambda: login)
        monkeypatch.setattr(login_cli, "test_login", lambda _login: ["gpt-5.5", "gpt-5.3-codex-spark"])
        monkeypatch.setattr(login_cli, "_input", lambda *a, **k: typed)

        assert login_cli._run_codex_login("openai-codex") == 0
        assert logins.load_model_cache()["openai-codex"] == [
            "gpt-5.5", "gpt-5.3-codex-spark", "gpt-5.4", "gpt-5.4-mini",
        ]

        capsys.readouterr()
        assert cli.main(["--list-models", "openai-codex"]) == 0
        out = capsys.readouterr().out
        assert out == (
            "openai-codex/gpt-5.5\n"
            "openai-codex/gpt-5.3-codex-spark\n"
            "openai-codex/gpt-5.4\n"
            "openai-codex/gpt-5.4-mini\n"
        )
    finally:
        _reset_logins()


def test_dialect_map_tags_anthropic_models():
    # claude-* on opencode is anthropic-dialect; glm-* is openai. Cosmetic tag,
    # but it must read provider_config.npm correctly off the live catalog.
    dialects = login_cli._dialect_map("opencode-go")
    assert dialects.get("claude-opus-4-8") == "anthropic"
    assert dialects.get("glm-5.2") == "openai"


def test_mask_hides_short_and_boundary_length_keys():
    # len<=12 can't show an 8-char prefix + 4-char suffix without revealing
    # every character (8 + 4 = 12), so anything that short falls back to
    # all-asterisks instead of a fake-looking partial reveal.
    assert login_cli._mask("short") == "*****"
    assert login_cli._mask("sk-12345678x") == "*" * 12  # 12 chars: full overlap
    assert login_cli._mask("x" * 11) == "*" * 11


def test_mask_reveals_edges_only_once_a_hidden_middle_exists():
    masked = login_cli._mask("sk-1234567890abcd")  # 17 chars: 8 prefix + 5 hidden + 4 suffix
    assert masked == "sk-12345*******abcd"
    assert "67890" not in masked  # the hidden middle chars never leak


def test_post_fetch_confirmation_skips_prompt_when_listing_validates_auth(monkeypatch):
    # deepseek (models_list_validates_auth=True): no test-choice prompt at all —
    # asking "add without a test?" when listing already proved the key works
    # is a prompt with only one sane answer, so it's skipped outright.
    monkeypatch.setattr(
        login_cli, "_secondary_test_choice",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not prompt")),
    )
    login = logins.Login(provider_id="deepseek", provider_api_key="sk-test")
    provider = providers.provider_for_login("deepseek")
    assert login_cli._post_fetch_confirmation(login, provider, ["deepseek-chat"]) is True


def test_post_fetch_confirmation_still_offers_test_when_listing_does_not_validate(monkeypatch):
    # opencode-go (models_list_validates_auth=False): the offer is still real.
    calls = []

    def fake_choice(models, *, require_test):
        calls.append(require_test)
        return True

    monkeypatch.setattr(login_cli, "_secondary_test_choice", fake_choice)
    login = logins.Login(provider_id="opencode-go", provider_api_key="k")
    provider = providers.provider_for_login("opencode-go")
    assert login_cli._post_fetch_confirmation(login, provider, ["glm-5.2"]) is True
    assert calls == [True]


def test_run_secondary_test_adds_on_success_without_a_further_confirm(monkeypatch):
    # The answer coming back IS the confirmation now — no more "hit enter to
    # add" after it; a stray input() call here would mean the prompt is back.
    monkeypatch.setattr(
        "builtins.input", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no further prompt expected"))
    )

    class _Result:
        text = "2"

    monkeypatch.setattr(login_cli.model_client, "stream_model", lambda **kwargs: _Result())
    login = logins.Login(provider_id="deepseek", provider_api_key="sk-test")
    provider = providers.provider_for_login("deepseek")
    assert login_cli._run_secondary_test(login, provider, "deepseek-chat") is True


def test_run_secondary_test_returns_false_on_failure_without_prompting(monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("nope")

    monkeypatch.setattr(login_cli.model_client, "stream_model", boom)
    login = logins.Login(provider_id="deepseek", provider_api_key="sk-test")
    provider = providers.provider_for_login("deepseek")
    assert login_cli._run_secondary_test(login, provider, "deepseek-chat") is False


def test_secondary_test_enter_adds_without_testing(monkeypatch):
    # Empty input = add without a test, in BOTH modes (require_test only warns).
    monkeypatch.setattr("builtins.input", lambda _prompt="": "")
    assert login_cli._secondary_test_choice(["first", "second"], require_test=True) is True
    assert login_cli._secondary_test_choice(["first", "second"], require_test=False) is True


def test_secondary_test_number_picks_that_model(monkeypatch):
    # A model number means "verify that one"; it returns the model id to test.
    monkeypatch.setattr("builtins.input", lambda _prompt="": "2")
    assert login_cli._secondary_test_choice(["first", "second"], require_test=True) == "second"


def test_run_logout_reports_corrupt_logins_file_cleanly_instead_of_raising(monkeypatch, capsys):
    def boom(_provider_id):
        raise logins.LoginsCorruptError("logins.toml is broken")

    monkeypatch.setattr(login_cli, "remove_login", boom)
    assert login_cli._run_logout("deepseek") == 1
    assert "logins.toml is broken" in capsys.readouterr().err


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
