from __future__ import annotations

import curses
import sys
from pathlib import Path

import pytest

from js import (
    cli,
    curses_menu,
    listings,
    login_cli,
    logins,
    messages as msgs,
    model_metadata,
    paths,
    providers,
)

BASE_URL_PROMPT = msgs.LOGIN_ASK_BASE_URL.text()
KEY_PROMPTS = {msgs.LOGIN_ASK_KEY.text(), msgs.LOGIN_ASK_KEY_OPTIONAL.text(), msgs.LOGIN_ASK_KEY_KEEP.text()}
EXTRA_MODELS_PROMPT = msgs.LOGIN_ASK_EXTRA_MODELS.text()


@pytest.fixture(autouse=True)
def in_own_directory(monkeypatch, tmp_path):
    """js starts in this test's directory, so no .env above the checkout
    fills in a provider's environment."""
    monkeypatch.chdir(tmp_path)


def _env_key_prompt(name: str, value: str) -> str:
    """The offer of the key in ENV:`name`, with PARANOID_MODE off (key shown)."""
    return msgs.LOGIN_ASK_ENV_KEY.text(name=name, key=value)


def _shown_env_key(prompt: str, name: str) -> str | None:
    """The key as the offer of ENV:`name` shows it, or None for another prompt."""
    before, after = msgs.LOGIN_ASK_ENV_KEY.text(name=name, key="\0").split("\0")
    if prompt.startswith(before) and prompt.endswith(after):
        return prompt[len(before):len(prompt) - len(after)]
    return None


def _reset_logins() -> None:
    logins.set_config_dir(paths.login_store_dir())


def _no_provider_env(monkeypatch, provider_id: str) -> None:
    provider = providers.provider_for_login(provider_id)
    for name in provider.api_key_env + provider.base_url_env + provider.model_env:
        monkeypatch.delenv(name, raising=False)


def _answers(key: str = "sk-typed", extras: str = "", custom_id: str | None = None):
    """A stand-in for the login prompts: the default for each, `key` for a key
    prompt, `extras` for the extra model ids, `custom_id` for a custom id."""

    def answer(prompt, *, default=None, secret=False):
        if prompt in KEY_PROMPTS:
            return key
        if prompt == EXTRA_MODELS_PROMPT:
            return extras
        if prompt == msgs.LOGIN_ASK_CUSTOM_ID.text():
            return custom_id
        return default if default is not None else ""

    return answer


def _fetch(monkeypatch, models=(), metadata=None) -> list[logins.Login]:
    """Each login `js --login` checks is recorded; the endpoint lists `models`."""
    fetched: list[logins.Login] = []

    def fetch(login):
        fetched.append(login)
        return list(models), dict(metadata or {})

    monkeypatch.setattr(login_cli, "test_login_with_metadata", fetch)
    return fetched


def _replies(monkeypatch, *replies: str) -> list[str]:
    """Plain `input()` (the test-first question) answers `replies` in order;
    the prompts it was asked are returned. One more question fails the test."""
    asked: list[str] = []
    pending = iter(replies)

    def reply(prompt=""):
        asked.append(prompt)
        try:
            return next(pending)
        except StopIteration:
            raise AssertionError(f"unexpected question: {prompt}") from None

    monkeypatch.setattr("builtins.input", reply)
    return asked


def _terminal(monkeypatch, *screens) -> None:
    """stdin and stdout are a terminal; each curses screen js opens is the
    next of `screens`."""
    pending = iter(screens)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(curses, "curs_set", lambda _n: None)
    monkeypatch.setattr(curses, "wrapper", lambda fn, *a, **k: fn(next(pending), *a, **k))


def _menus(monkeypatch, *choices) -> list[list[str]]:
    """Each menu `js --login` opens is recorded by its items and answered
    with the next of `choices`: an index, None, or a function of the items."""
    shown: list[list[str]] = []
    pending = iter(choices)

    def menu(_stdscr, items, title, **_kwargs):
        shown.append(list(items))
        choice = next(pending)
        return choice(items) if callable(choice) else choice

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(curses, "wrapper", lambda fn, *a, **k: fn(None, *a, **k))
    monkeypatch.setattr(curses_menu, "menu", menu)
    return shown


def test_login_cli_main_accepts_provider_argument(monkeypatch):
    _no_provider_env(monkeypatch, "deepseek")
    monkeypatch.setattr(login_cli, "_input", _answers())
    fetched = _fetch(monkeypatch)
    assert cli.main(["--login", "deepseek"]) == 0
    assert [login.provider_id for login in fetched] == ["deepseek"]


def test_login_cli_main_accepts_login_subcommand(monkeypatch):
    _no_provider_env(monkeypatch, "openai-completions")
    monkeypatch.setattr(login_cli, "_input", _answers())
    fetched = _fetch(monkeypatch)
    assert login_cli.main(["--login", "openai-completions"]) == 0
    assert [login.provider_id for login in fetched] == ["openai-completions"]


def test_login_cli_provider_list_includes_first_class_shortcuts():
    rows = {provider.id: provider.display_name for provider in providers.all_providers()}

    for provider_id in ("ollama", "llama.cpp", "mimo", "mimo-token-plan"):
        assert rows[provider_id].strip()


def test_login_registry_includes_modelsdotdev_provider(tmp_path: Path):
    logins.set_config_dir(tmp_path)
    try:
        rows = {provider.id: provider.display_name for provider in providers.login_providers()}
        assert rows["alibaba"] == "Alibaba"
        assert rows["openai"] == "OpenAI API"
    finally:
        _reset_logins()


def test_login_provider_rows_show_saved_env_and_registry(monkeypatch):
    logins.save_login(logins.Login(provider_id="deepseek", provider_api_key="sk-test"))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    for provider_id in ("deepseek", "alibaba"):
        _no_provider_env(monkeypatch, provider_id)
    # The manager's last row adds a registry provider; its picker closes, then the manager.
    shown = _menus(monkeypatch, lambda items: len(items) - 1, None, None)

    assert cli.main(["--login"]) == 0

    def row(provider_id, source):
        name = providers.get_provider(provider_id).display_name
        return msgs.LOGIN_PROVIDER_ROW.format(pid=provider_id, name=name, source=source)

    provider_picker = shown[1]
    assert row("deepseek", "saved") in provider_picker
    assert row("openai", "env") in provider_picker
    assert row("alibaba", "registry") in provider_picker


def test_login_picker_accepts_action_keymaps(monkeypatch):
    monkeypatch.setattr(login_cli.curses, "curs_set", lambda value: None)
    screen = _FakeStdscr([ord("z")])
    chosen = curses_menu._curses_picker(screen, [("first", "")], "choose",
                                      keymaps={"list": {"choose": (ord("z"),)}})
    assert chosen == [0]


def test_multiselect_accepts_action_keymaps(monkeypatch):
    monkeypatch.setattr(curses, "curs_set", lambda value: None)
    screen = _FakeStdscr([ord("z"), ord("q")])
    chosen = curses_menu.multiselect(screen, [("first", "")], "choose", preselected={0},
                                     keymaps={"list": {"choose": (ord("z"),)}})
    assert chosen == [0]


def test_collect_api_login_offers_env_key_and_accepting_uses_it(monkeypatch):
    # Login never takes an env key silently. It offers the env key;
    # answering yes uses it, and only then.
    _no_provider_env(monkeypatch, "deepseek")
    monkeypatch.setenv("JS_PARANOID_MODE", "off")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env-deepseek")
    prompts: list[str] = []

    def scripted_input(prompt, *, default=None, secret=False):
        prompts.append(prompt)
        if prompt == BASE_URL_PROMPT:
            return default
        if prompt == _env_key_prompt("DEEPSEEK_API_KEY", "sk-env-deepseek"):
            return "y"
        raise AssertionError(f"unexpected prompt: {prompt}")

    monkeypatch.setattr(login_cli, "_input", scripted_input)
    fetched = _fetch(monkeypatch)
    assert cli.main(["--login", "deepseek"]) == 0
    [login] = fetched
    assert login.provider_api_key == "sk-env-deepseek"
    assert login.provider_base_url == "https://api.deepseek.com"
    assert _env_key_prompt("DEEPSEEK_API_KEY", "sk-env-deepseek") in prompts


def test_collect_api_login_declining_env_key_prompts_for_one(monkeypatch):
    _no_provider_env(monkeypatch, "deepseek")
    monkeypatch.setenv("JS_PARANOID_MODE", "off")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env-decoy")

    def scripted_input(prompt, *, default=None, secret=False):
        if prompt == BASE_URL_PROMPT:
            return default
        if prompt == _env_key_prompt("DEEPSEEK_API_KEY", "sk-env-decoy"):
            return "n"
        if prompt in KEY_PROMPTS:
            return "sk-typed-real"
        raise AssertionError(f"unexpected prompt: {prompt}")

    monkeypatch.setattr(login_cli, "_input", scripted_input)
    fetched = _fetch(monkeypatch)
    assert cli.main(["--login", "deepseek"]) == 0
    [login] = fetched
    assert login.provider_api_key == "sk-typed-real"


@pytest.mark.parametrize("provider_id", ["deepseek", "vllm"])
def test_cancelled_api_key_prompt_cancels_login(monkeypatch, provider_id):
    _no_provider_env(monkeypatch, provider_id)
    if provider_id == "deepseek":
        logins.save_login(logins.Login(provider_id=provider_id, provider_api_key="saved-key"))
    replies = iter(["", KeyboardInterrupt()])

    def reply(_prompt):
        value = next(replies)
        if isinstance(value, KeyboardInterrupt):
            raise value
        return value

    monkeypatch.setattr("builtins.input", reply)
    fetched = _fetch(monkeypatch)

    assert cli.main(["--login", provider_id]) == 0
    assert fetched == []


def test_empty_api_key_reply_answers_instead_of_cancelling(monkeypatch):
    # A bare Enter is an answer that takes the default; only cancelling the
    # prompt cancels. "Enter for none" and "Enter keeps the saved one" must
    # stay answers.
    logins.save_login(logins.Login(provider_id="deepseek", provider_api_key="saved-key"))
    for provider_id in ("deepseek", "vllm"):
        _no_provider_env(monkeypatch, provider_id)

    def enter(_prompt):
        return ""

    monkeypatch.setattr("builtins.input", enter)
    monkeypatch.setattr(login_cli, "getpass", enter)
    fetched = _fetch(monkeypatch)
    assert cli.main(["--login", "deepseek"]) == 0
    assert cli.main(["--login", "vllm"]) == 0
    kept, keyless = fetched
    assert kept.provider_api_key == "saved-key"
    assert keyless.provider_id == "vllm"
    assert keyless.provider_api_key


def test_collect_api_login_enter_keeps_empty_base_url_for_official_provider(monkeypatch):
    _no_provider_env(monkeypatch, "openai")
    prompted: dict[str, str | None] = {}

    def fake_input(prompt, *, default=None, secret=False):
        prompted[prompt] = default
        if prompt == BASE_URL_PROMPT:
            return default
        if prompt in KEY_PROMPTS:
            return "sk-test"
        raise AssertionError(f"unexpected prompt: {prompt}")

    monkeypatch.setattr(login_cli, "_input", fake_input)
    fetched = _fetch(monkeypatch)
    assert cli.main(["--login", "openai"]) == 0
    [login] = fetched
    assert prompted[BASE_URL_PROMPT] == ""
    assert login.provider_base_url is None
    assert login.provider_api_key == "sk-test"


def test_collect_api_login_variable_provider_prompts_with_default_and_keeps_enter(monkeypatch):
    _no_provider_env(monkeypatch, "vllm")
    prompted: dict[str, str | None] = {}

    def fake_input(prompt, *, default=None, secret=False):
        prompted[prompt] = default
        if prompt == BASE_URL_PROMPT:
            return default
        if prompt in KEY_PROMPTS:
            return ""
        raise AssertionError(f"unexpected prompt: {prompt}")

    monkeypatch.setattr(login_cli, "_input", fake_input)
    fetched = _fetch(monkeypatch)
    assert cli.main(["--login", "vllm"]) == 0
    [login] = fetched
    assert prompted[BASE_URL_PROMPT] == "http://127.0.0.1:8000/v1"
    assert login.provider_base_url == "http://127.0.0.1:8000/v1"


def test_collect_api_login_variable_provider_prompts_even_with_env_base(monkeypatch):
    _no_provider_env(monkeypatch, "vllm")
    monkeypatch.setenv("VLLM_BASE_URL", "http://gpu-box.test/v1")
    prompted: dict[str, str | None] = {}

    def fake_input(prompt, *, default=None, secret=False):
        prompted[prompt] = default
        if prompt == BASE_URL_PROMPT:
            return default
        if prompt in KEY_PROMPTS:
            return ""
        raise AssertionError(f"unexpected prompt: {prompt}")

    monkeypatch.setattr(login_cli, "_input", fake_input)
    fetched = _fetch(monkeypatch)
    assert cli.main(["--login", "vllm"]) == 0
    [login] = fetched
    assert prompted[BASE_URL_PROMPT] == "http://gpu-box.test/v1"
    assert login.provider_base_url == "http://gpu-box.test/v1"


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


def test_run_login_caches_server_model_metadata(monkeypatch):
    _no_provider_env(monkeypatch, "vllm")

    def fake_input(prompt, *, default=None, secret=False):
        if prompt == BASE_URL_PROMPT:
            return default
        if prompt in KEY_PROMPTS:
            return ""
        raise AssertionError(f"unexpected prompt: {prompt}")

    metadata = {
        "served-model": logins.ModelCacheMetadata(
            context_window=32768,
            max_output_tokens=4096,
            training_context_window=262144,
        )
    }
    monkeypatch.setattr(login_cli, "_input", fake_input)
    _fetch(monkeypatch, ["served-model"], metadata)

    assert cli.main(["--login", "vllm"]) == 0
    assert logins.load_model_cache()["vllm"] == ["served-model"]
    cached = logins.load_model_cache_metadata()["vllm"]["served-model"]
    assert cached.context_window == 32768
    assert cached.max_output_tokens == 4096
    assert cached.training_context_window == 262144


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
    return curses_menu.multiselect(
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
    _no_provider_env(monkeypatch, "opencode-go")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(login_cli, "_input", _answers())
    _replies(monkeypatch, "")
    models = ["glm-5.2", "glm-5.1", "kimi-k2.7-code"]
    _fetch(monkeypatch, models)
    assert cli.main(["--login", "opencode-go"]) == 0
    assert logins.load_model_cache()["opencode-go"] == models


def test_models_edit_removes_and_adds_models_without_login_fetch_or_catalog_refresh(
    monkeypatch, tmp_path: Path,
):
    logins.set_config_dir(tmp_path)
    metadata = {
        "keep": logins.ModelCacheMetadata(context_window=32768),
        "remove": logins.ModelCacheMetadata(context_window=65536),
    }
    logins.cache_models("ollama", ["keep", "remove"], metadata=metadata)
    keys = [curses.KEY_DOWN, ord(" "), ord("\n")]
    _terminal(monkeypatch, _FakeStdscr(keys))
    pickers = []
    multiselect = curses_menu.multiselect

    def recording_multiselect(stdscr, rows, title, **kwargs):
        pickers.append(([row[0] for row in rows], kwargs["preselected"]))
        return multiselect(stdscr, rows, title, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("network/login/catalog call")

    def only_extra_models(prompt, **_kwargs):
        assert prompt == EXTRA_MODELS_PROMPT, f"login prompt during models-edit: {prompt}"
        return "added"

    refreshes = []
    monkeypatch.setattr(curses_menu, "multiselect", recording_multiselect)
    monkeypatch.setattr(login_cli, "_input", only_extra_models)
    monkeypatch.setattr(login_cli, "test_login_with_metadata", forbidden)
    monkeypatch.setattr(login_cli.model_client, "stream_model", forbidden)
    monkeypatch.setattr(model_metadata, "ensure_fresh_catalog", lambda *a, **k: refreshes.append(a))

    assert cli.main(["--models-edit", "ollama-local"]) == 0
    assert pickers == [(["keep", "remove"], {0, 1})]
    assert refreshes == []
    assert logins.load_model_cache()["ollama"] == ["keep", "added"]
    assert logins.load_model_cache_metadata()["ollama"] == {"keep": metadata["keep"]}


def test_models_edit_missing_or_empty_cache_leaves_file_unchanged(monkeypatch, tmp_path: Path):
    logins.set_config_dir(tmp_path)
    cache_path = tmp_path / "models-cache.json"
    cache_path.write_text('{"deepseek": []}\n', encoding="utf-8")
    before = cache_path.read_bytes()
    _terminal(monkeypatch)
    monkeypatch.setattr(curses, "wrapper", lambda *_args, **_kwargs: pytest.fail("picker should not open"))

    assert cli.main(["--models-edit", "deepseek"]) == 1
    assert cache_path.read_bytes() == before

    cache_path.unlink()
    assert cli.main(["--models-edit", "not-saved"]) == 1
    assert not cache_path.exists()


def test_models_edit_picker_cancel_leaves_cache_unchanged(monkeypatch, tmp_path: Path):
    logins.set_config_dir(tmp_path)
    metadata = {"keep": logins.ModelCacheMetadata(max_output_tokens=4096)}
    logins.cache_models("deepseek", ["keep"], metadata=metadata)
    cache_path = tmp_path / "models-cache.json"
    before = cache_path.read_bytes()
    _terminal(monkeypatch, _FakeStdscr([ord("q")]))

    assert cli.main(["--models-edit", "deepseek"]) == 0
    assert cache_path.read_bytes() == before


def test_select_models_interactive_plain_enter_keeps_single_fetched_model(monkeypatch):
    _no_provider_env(monkeypatch, "vllm")
    keys = [ord("\n")]  # confirm immediately, default selection keeps the model
    _terminal(monkeypatch, _FakeStdscr(keys))
    monkeypatch.setattr(login_cli, "_input", _answers(key=""))
    _fetch(monkeypatch, ["owner-model"])
    assert cli.main(["--login", "vllm"]) == 0
    assert logins.load_model_cache()["vllm"] == ["owner-model"]


def _recording_keep_list(monkeypatch, keys) -> dict[str, object]:
    """The keep-list picker runs on `keys`; its rows and preselection are
    recorded."""
    captured: dict[str, object] = {}
    multiselect = curses_menu.multiselect

    def recording_multiselect(stdscr, rows, title, **kwargs):
        captured["rows"] = [row[0] for row in rows]
        captured["annotations"] = dict(rows)
        captured["preselected"] = set(kwargs["preselected"])
        return multiselect(stdscr, rows, title, **kwargs)

    _terminal(monkeypatch, _FakeStdscr(keys))
    monkeypatch.setattr(curses_menu, "multiselect", recording_multiselect)
    return captured


def test_relogin_plain_enter_drops_cached_models_not_refetched(monkeypatch):
    _no_provider_env(monkeypatch, "openai")
    logins.cache_models("openai", ["old-model"])
    captured = _recording_keep_list(monkeypatch, [ord("\n")])
    monkeypatch.setattr(login_cli, "_input", _answers(key="sk"))
    _fetch(monkeypatch, ["new-model"])

    assert cli.main(["--login", "openai"]) == 0
    assert logins.load_model_cache()["openai"] == ["new-model"]
    assert (captured["rows"], captured["preselected"]) == (["new-model", "old-model"], {0})


def test_select_models_interactive_with_custom_add(monkeypatch):
    _no_provider_env(monkeypatch, "opencode-go")
    # All preselected: deselect row0 and row2.
    keys = [ord(" "), curses.KEY_DOWN, curses.KEY_DOWN, ord(" "), ord("\n")]
    _terminal(monkeypatch, _FakeStdscr(keys))
    monkeypatch.setattr(login_cli, "_input", _answers(extras="extra-model, glm-5.2"))
    _replies(monkeypatch, "")
    _fetch(monkeypatch, ["glm-5.2", "glm-5", "kimi-k2.7-code"])
    assert cli.main(["--login", "opencode-go"]) == 0
    # row1 selected; typed ids are explicit additions, even if one appeared in the fetched list.
    assert logins.load_model_cache()["opencode-go"] == ["glm-5", "extra-model", "glm-5.2"]


def test_select_models_typed_extras_dedupe_against_selected_fetched(monkeypatch):
    _no_provider_env(monkeypatch, "openai")
    _terminal(monkeypatch, _FakeStdscr([ord("\n")]))
    monkeypatch.setattr(login_cli, "_input", _answers(extras="gpt-5.5,gpt-5.4,gpt-5.4-mini"))
    _fetch(monkeypatch, ["gpt-5.5"])
    assert cli.main(["--login", "openai"]) == 0
    assert logins.load_model_cache()["openai"] == ["gpt-5.5", "gpt-5.4", "gpt-5.4-mini"]


def test_login_typed_extras_survive_cache_and_list_models(monkeypatch, capsys):
    _no_provider_env(monkeypatch, "openai")
    _terminal(monkeypatch, _FakeStdscr([ord("\n")]))
    typed = "gpt-5.4,gpt-5.3-codex-spark,gpt-5.4-mini"
    monkeypatch.setattr(login_cli, "_input", _answers(extras=typed))
    _fetch(monkeypatch, ["gpt-5.5", "gpt-5.3-codex-spark"])

    assert cli.main(["--login", "openai"]) == 0
    assert logins.load_model_cache()["openai"] == [
        "gpt-5.5", "gpt-5.3-codex-spark", "gpt-5.4", "gpt-5.4-mini",
    ]

    capsys.readouterr()
    assert listings.print_model_list("openai", None) == 0
    out = capsys.readouterr().out
    assert out == (
        "openai/gpt-5.5\n"
        "openai/gpt-5.3-codex-spark\n"
        "openai/gpt-5.4\n"
        "openai/gpt-5.4-mini\n"
    )


def test_dialect_map_tags_anthropic_models(monkeypatch):
    # claude-* on opencode is anthropic-dialect; glm-* is openai. Cosmetic tag
    # on the keep-list row, but it must read provider_config.npm correctly off
    # the live catalog.
    _no_provider_env(monkeypatch, "opencode-go")
    captured = _recording_keep_list(monkeypatch, [ord("\n")])
    monkeypatch.setattr(login_cli, "_input", _answers())
    _replies(monkeypatch, "")
    _fetch(monkeypatch, ["claude-opus-4-8", "glm-5.2"])
    assert cli.main(["--login", "opencode-go"]) == 0
    assert captured["annotations"]["claude-opus-4-8"] == "anthropic"
    assert captured["annotations"]["glm-5.2"] == "openai"


def _env_key_as_shown(monkeypatch, key: str) -> str:
    """The key in DEEPSEEK_API_KEY as `js --login deepseek` shows it when it
    offers that key; the login is cancelled there."""
    _no_provider_env(monkeypatch, "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", key)
    shown: list[str] = []

    def scripted_input(prompt, *, default=None, secret=False):
        if prompt == BASE_URL_PROMPT:
            return default
        displayed = _shown_env_key(prompt, "DEEPSEEK_API_KEY")
        assert displayed is not None, f"unexpected prompt: {prompt}"
        shown.append(displayed)
        return None

    monkeypatch.setattr(login_cli, "_input", scripted_input)
    assert cli.main(["--login", "deepseek"]) == 0
    [displayed] = shown
    return displayed


def test_mask_does_not_reveal_complete_keys_even_across_separators(monkeypatch):
    monkeypatch.setenv("JS_PARANOID_MODE", "on")
    for key in ("short", "sk-12345678x", "x" * 11, "sk-1234567890abcd"):
        masked = _env_key_as_shown(monkeypatch, key)
        assert masked
        # A prefix + separator + suffix must not expose every character of a
        # short key. Checking contiguous substrings would miss that regression.
        displayed = iter(masked)
        assert not all(character in displayed for character in key)


def test_post_fetch_confirmation_skips_prompt_when_listing_validates_auth(monkeypatch):
    # deepseek (models_list_validates_auth=True): no test-choice prompt at all —
    # asking "add without a test?" when listing already proved the key works
    # is a prompt with only one sane answer, so it's skipped outright.
    _no_provider_env(monkeypatch, "deepseek")
    monkeypatch.setattr(login_cli, "_input", _answers())
    asked = _replies(monkeypatch)
    _fetch(monkeypatch, ["deepseek-chat"])
    assert cli.main(["--login", "deepseek"]) == 0
    assert asked == []
    assert logins.load_model_cache()["deepseek"] == ["deepseek-chat"]


def test_post_fetch_confirmation_still_offers_test_when_listing_does_not_validate(monkeypatch, capsys):
    # opencode-go (models_list_validates_auth=False): the offer is still real.
    _no_provider_env(monkeypatch, "opencode-go")
    monkeypatch.setattr(login_cli, "_input", _answers())
    asked = _replies(monkeypatch, "")
    _fetch(monkeypatch, ["glm-5.2"])
    assert cli.main(["--login", "opencode-go"]) == 0
    assert asked == [msgs.LOGIN_ASK_TEST.text()]
    assert msgs.LOGIN_LISTING_NOT_PROOF.text() in capsys.readouterr().out
    assert logins.load_model_cache()["opencode-go"] == ["glm-5.2"]


def test_run_secondary_test_adds_on_success_without_a_further_confirm(monkeypatch):
    # The answer coming back IS the confirmation now — no more "hit enter to
    # add" after it; a further input() call here would mean the prompt is back.
    _no_provider_env(monkeypatch, "opencode-go")
    monkeypatch.setattr(login_cli, "_input", _answers())
    asked = _replies(monkeypatch, "1")

    class _Result:
        text = "2"

    monkeypatch.setattr(login_cli.model_client, "stream_model", lambda **kwargs: _Result())
    _fetch(monkeypatch, ["glm-5.2"])
    assert cli.main(["--login", "opencode-go"]) == 0
    assert len(asked) == 1
    assert logins.load_model_cache()["opencode-go"] == ["glm-5.2"]


def test_run_secondary_test_returns_false_on_failure_without_prompting(monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("nope")

    _no_provider_env(monkeypatch, "opencode-go")
    monkeypatch.setattr(login_cli, "_input", _answers())
    asked = _replies(monkeypatch, "1")
    monkeypatch.setattr(login_cli.model_client, "stream_model", boom)
    _fetch(monkeypatch, ["glm-5.2"])
    assert cli.main(["--login", "opencode-go"]) == 1
    assert len(asked) == 1
    assert "opencode-go" not in logins.load_logins()


def test_secondary_test_enter_adds_without_testing(monkeypatch):
    # Empty input = add without a test.
    _no_provider_env(monkeypatch, "opencode-go")
    monkeypatch.setattr(login_cli, "_input", _answers())
    _replies(monkeypatch, "")
    monkeypatch.setattr(login_cli.model_client, "stream_model",
                        lambda **kwargs: pytest.fail("Enter ran the test"))
    _fetch(monkeypatch, ["first", "second"])
    assert cli.main(["--login", "opencode-go"]) == 0
    assert logins.load_model_cache()["opencode-go"] == ["first", "second"]


def test_secondary_test_enter_adds_without_testing_when_no_test_is_required(monkeypatch):
    # The login flow always asks with require_test=True; the False branch is
    # reached only from here.
    monkeypatch.setattr("builtins.input", lambda _prompt="": "")
    assert login_cli._secondary_test_choice(["first", "second"], require_test=False) is True


def test_secondary_test_number_picks_that_model(monkeypatch):
    # A model number means "verify that one": the test asks that model.
    _no_provider_env(monkeypatch, "opencode-go")
    monkeypatch.setattr(login_cli, "_input", _answers())
    _replies(monkeypatch, "2")
    tested: list[str] = []

    class _Result:
        text = "2"

    def stream_model(**kwargs):
        tested.append(kwargs["model_id"])
        return _Result()

    monkeypatch.setattr(login_cli.model_client, "stream_model", stream_model)
    _fetch(monkeypatch, ["first", "second"])
    assert cli.main(["--login", "opencode-go"]) == 0
    assert tested == ["second"]


def test_run_logout_reports_corrupt_logins_file_cleanly_instead_of_raising(monkeypatch, capsys):
    def boom(_provider_id):
        raise logins.LoginsCorruptError("logins.toml is broken")

    monkeypatch.setattr(login_cli, "remove_login", boom)
    assert cli.main(["--logout", "deepseek"]) == 1
    assert "logins.toml is broken" in capsys.readouterr().err


def test_login_cli_logout_requires_provider():
    assert login_cli.main(["logout"]) == 2


def test_login_cli_logout_dispatch():
    logins.save_login(logins.Login(provider_id="deepseek", provider_api_key="sk-test"))
    assert login_cli.main(["logout", "deepseek"]) == 0
    assert "deepseek" not in logins.load_logins()


def test_top_level_cli_dispatches_login(monkeypatch):
    captured = {}

    def fake_login_main(args):
        captured["args"] = args
        return 0

    monkeypatch.setattr(login_cli, "main", fake_login_main)
    monkeypatch.setattr(cli, "login_cli", login_cli, raising=False)
    assert cli.main(["--login", "deepseek"]) == 0
    assert captured == {"args": ["deepseek"]}


def test_top_level_cli_dispatches_models_edit(monkeypatch):
    captured = {}

    def fake_login_main(args):
        captured["args"] = args
        return 0

    monkeypatch.setattr(login_cli, "main", fake_login_main)
    monkeypatch.setattr(cli, "login_cli", login_cli, raising=False)
    assert cli.main(["--models-edit", "deepseek"]) == 0
    assert captured == {"args": ["models-edit", "deepseek"]}


def test_top_level_cli_dispatches_models_edit_from_console_argv(monkeypatch):
    captured = {}

    def fake_login_main(args):
        captured["args"] = args
        return 0

    monkeypatch.setattr(login_cli, "main", fake_login_main)
    monkeypatch.setattr(cli, "login_cli", login_cli, raising=False)
    monkeypatch.setattr(sys, "argv", ["js", "--models-edit", "deepseek"])

    assert cli.main() == 0
    assert captured == {"args": ["models-edit", "deepseek"]}


def test_readd_with_new_shape_uses_the_new_sdk(monkeypatch):
    """Re-adding a saved custom provider under a new shape: the freshly picked
    sdk wins over the saved one, same order as the shape."""
    logins.save_login(logins.Login(
        provider_id="mybox", provider_api_key="sk", provider_base_url="http://box.test/v1",
        sdk_provider_id="openai", shape_provider_id="openai-completions",
    ))

    def shape(items):
        return next(i for i, item in enumerate(items) if item.startswith("anthropic-custom"))

    # The manager's first row adds a custom provider; then its shape; then the manager closes.
    _menus(monkeypatch, 0, shape, None)
    monkeypatch.setattr(login_cli, "_input", _answers(custom_id="mybox"))
    fetched = _fetch(monkeypatch)
    assert cli.main(["--login"]) == 0
    [login] = fetched
    assert login.shape_provider_id == "anthropic-custom"
    assert login.sdk_provider_id == "anthropic"


def test_login_without_a_terminal_fails_with_one_clear_error(monkeypatch, capsys):
    """Every picker needs stdin and stdout to be a terminal; without one `js
    --login` exits 2 with a message instead of a curses traceback."""

    class Stream:
        def __init__(self, tty):
            self.tty = tty

        def isatty(self):
            return self.tty

        def write(self, _text):
            return 0

        def flush(self):
            pass

    monkeypatch.setattr(curses, "wrapper", lambda *a, **k: (_ for _ in ()).throw(AssertionError("curses ran")))
    for stdin_tty, stdout_tty in ((False, True), (True, False)):
        monkeypatch.setattr(sys, "stdin", Stream(stdin_tty))
        monkeypatch.setattr(sys, "stdout", Stream(stdout_tty))
        assert login_cli.main([]) == 2
        err = capsys.readouterr().err
        assert "interactive terminal" in err and "Traceback" not in err


def test_login_without_a_terminal_keeps_every_fetched_model(monkeypatch):
    _no_provider_env(monkeypatch, "deepseek")
    monkeypatch.setattr(curses, "wrapper", lambda *a, **k: pytest.fail("curses ran"))
    monkeypatch.setattr(login_cli, "_input", _answers())
    _fetch(monkeypatch, ["m1", "m2"])
    assert cli.main(["--login", "deepseek"]) == 0
    assert logins.load_model_cache()["deepseek"] == ["m1", "m2"]


@pytest.mark.parametrize("picker", ["provider", "shape"])
def test_a_picker_after_the_terminal_went_away_refuses(monkeypatch, capsys, picker):
    """The registry provider picker and the API shape picker refuse once
    stdin is no longer a terminal, as the manager does."""

    def leave_terminal(choice):
        def choose(items):
            monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
            return choice(items)
        return choose

    # The manager's last row opens the registry picker; its first adds a
    # custom provider, which asks for an API shape.
    _menus(monkeypatch, leave_terminal((lambda items: len(items) - 1) if picker == "provider" else (lambda items: 0)))
    monkeypatch.setattr(login_cli, "_input", _answers(custom_id="mybox"))
    assert cli.main(["--login"]) == 2
    err = capsys.readouterr().err
    assert "interactive terminal" in err and "Traceback" not in err


def test_run_login_saves_a_server_whose_model_list_cannot_be_fetched(monkeypatch):
    _no_provider_env(monkeypatch, "vllm")

    def fake_input(prompt, *, default=None, secret=False):
        if prompt == BASE_URL_PROMPT:
            return "http://down.example:8000/v1"
        if prompt in KEY_PROMPTS:
            return ""
        raise AssertionError(f"unexpected prompt: {prompt}")

    def unreachable(_login):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(login_cli, "_input", fake_input)
    monkeypatch.setattr(login_cli, "test_login_with_metadata", unreachable)

    assert cli.main(["--login", "vllm"]) == 0
    assert logins.load_logins()["vllm"].provider_base_url == "http://down.example:8000/v1"


def test_login_defaults_survive_a_save_and_load(tmp_path: Path):
    logins.set_config_dir(tmp_path)
    try:
        logins.save_login(logins.Login(
            provider_id="vllm", provider_base_url="http://x/v1",
            defaults={"CONTEXT_SIZE": 32768, "VISION": True},
            model_defaults={"org/model-a": {"TEMPERATURE": 0.6}},
        ))
        logins.set_config_dir(tmp_path)  # drop any cached parse
        login = logins.load_logins_fresh()["vllm"]
        assert login.defaults == {"CONTEXT_SIZE": 32768, "VISION": True}
        assert login.model_defaults == {"org/model-a": {"TEMPERATURE": 0.6}}
    finally:
        _reset_logins()


def test_e_on_a_kept_model_saves_its_defaults(monkeypatch):
    class _Screen(_FakeStdscr):
        def move(self, *_args):
            pass

        def clrtoeol(self):
            pass

    logins.save_login(logins.Login(provider_id="vllm", provider_base_url="http://x/v1"))
    logins.cache_models("vllm", ["model-a", "model-b"])
    typed = [ord(ch) for ch in "temperature=0.6"]
    keys = [curses.KEY_DOWN, ord("e"), *typed, ord("\n"), ord("\n")]
    _terminal(monkeypatch, _Screen(keys))
    monkeypatch.setattr(login_cli, "_input", _answers())

    assert cli.main(["--models-edit", "vllm"]) == 0
    assert logins.load_model_cache()["vllm"] == ["model-a", "model-b"]
    assert logins.load_logins_fresh()["vllm"].model_defaults == {"model-b": {"TEMPERATURE": 0.6}}


def test_logging_in_again_keeps_the_saved_defaults(monkeypatch):
    _no_provider_env(monkeypatch, "vllm")
    logins.save_login(logins.Login(provider_id="vllm", provider_base_url="http://x/v1",
                                   defaults={"CONTEXT_SIZE": 4096},
                                   model_defaults={"m": {"TEMPERATURE": 0.5}}))

    def fake_input(prompt, *, default=None, secret=False):
        if prompt == BASE_URL_PROMPT or prompt in KEY_PROMPTS:
            return default
        raise AssertionError(f"unexpected prompt: {prompt}")

    def unreachable(_login):
        raise ConnectionError("down")

    monkeypatch.setattr(login_cli, "_input", fake_input)
    monkeypatch.setattr(login_cli, "test_login_with_metadata", unreachable)
    assert cli.main(["--login", "vllm"]) == 0
    login = logins.load_logins_fresh()["vllm"]
    assert login.defaults == {"CONTEXT_SIZE": 4096}
    assert login.model_defaults == {"m": {"TEMPERATURE": 0.5}}


def test_effort_off_survives_a_save_and_load():
    from js import login_defaults

    values = login_defaults.parse("EFFORT=off")
    assert login_defaults.checked(values) == values


def test_registered_local_placeholder_is_used_for_keyless_login(monkeypatch):
    _no_provider_env(monkeypatch, "vllm")
    monkeypatch.setattr(login_cli, "_input", lambda _prompt, default=None, **kw: default or "")
    fetched = _fetch(monkeypatch)
    assert cli.main(["--extra", "LOCAL_API_KEY_PLACEHOLDER=local-token", "--login", "vllm"]) == 0
    [login] = fetched
    assert login.provider_api_key == "local-token"
