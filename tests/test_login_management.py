"""Provider management uses real stores and headless terminal input."""
import curses
import re

import pytest

from js import login_cli, logins


class Screen:
    def __init__(self, keys):
        self.keys = iter(keys)
        self.rendered = []

    def keypad(self, flag):
        pass

    def clear(self):
        pass

    def getmaxyx(self):
        return 24, 100

    def addstr(self, y, x, text):
        self.rendered.append(text)

    def refresh(self):
        pass

    def getch(self):
        return next(self.keys)


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(logins, "_CONFIG_DIR_OVERRIDE", tmp_path)
    monkeypatch.setattr(curses, "curs_set", lambda _: None)
    # Pickers refuse without a terminal; these tests drive them through a fake
    # curses screen, so they stand in for one.
    monkeypatch.setattr(login_cli, "_interactive", lambda: True)


def test_registry_picker_accepts_direct_typeahead(monkeypatch):
    monkeypatch.setattr(curses, "wrapper", lambda fn, *a, **kw: fn(Screen(map(ord, "de\n")), *a, **kw))
    assert login_cli._select_provider() == "deepseek"


def test_rename_preserves_login_and_cache_metadata(tmp_path):
    from dataclasses import replace

    original = logins.Login("mine", sdk_provider_id="openai", shape_provider_id="openai-responses",
                            provider_api_key="secret", provider_headers={"X-Key": "value"},
                            defaults={"TEMPERATURE": 0.5}, model_defaults={"model": {"TEMPERATURE": 0.2}})
    logins.save_login(original)
    metadata = {"model": logins.ModelCacheMetadata(context_window=32768)}
    logins.cache_models("mine", ["model"], metadata=metadata)
    assert logins.rename_login("mine", "renamed")
    assert logins.load_logins() == {"renamed": replace(original, provider_id="renamed")}
    assert logins.load_model_cache() == {"renamed": ["model"]}
    assert logins.load_model_cache_metadata() == {"renamed": metadata}


@pytest.mark.parametrize("target", ["taken", "TAKEN", "bad/name", "'cpa", "", "openai"])
def test_rename_rejects_collisions_and_invalid_names(target, tmp_path):
    logins.save_login(logins.Login("mine"))
    logins.save_login(logins.Login("taken"))
    logins.cache_models("mine", ["model"])
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    with pytest.raises(ValueError):
        logins.rename_login("mine", target)
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()} == before


def test_invalid_custom_provider_name_stops_before_shape_selection(monkeypatch):
    monkeypatch.setattr(login_cli, "_input", lambda *a, **kw: "'cpa")
    monkeypatch.setattr(login_cli, "_select_api_shape", lambda: pytest.fail("invalid name reached API shape selection"))
    assert login_cli._ask_custom_provider() is None
    assert logins.load_logins() == {}


def test_rename_repairs_malformed_source_provider():
    from dataclasses import replace

    original = logins.Login("'cpa", sdk_provider_id="openai", shape_provider_id="cliproxyapi",
                            provider_api_key="secret", provider_base_url="http://localhost:8317/v1")
    logins.save_login(original)
    original = logins.load_logins()["'cpa"]
    metadata = {"gpt": logins.ModelCacheMetadata(context_window=32768)}
    logins.cache_models("'cpa", ["gpt"], metadata=metadata)
    assert logins.rename_login("'cpa", "cpa")
    assert logins.load_logins() == {"cpa": replace(original, provider_id="cpa")}
    assert logins.load_model_cache() == {"cpa": ["gpt"]}
    assert logins.load_model_cache_metadata() == {"cpa": metadata}


def test_rename_registered_provider_preserves_endpoint_and_transport():
    from js import providers

    original = providers.get_provider("deepseek")
    logins.save_login(logins.Login("deepseek", provider_api_key="secret"))
    assert logins.rename_login("deepseek", "my-deepseek")
    renamed = logins.load_logins()["my-deepseek"]
    assert renamed.sdk_provider_id == original.effective_sdk_provider_id
    assert renamed.provider_base_url == original.default_base_url
    assert providers.get_provider("my-deepseek").transport == original.transport


def test_rename_preserves_unknown_fields_and_cache_records(tmp_path):
    import json
    import tomllib

    (tmp_path / "logins.toml").write_text('[mine]\nprovider_api_key = "secret"\nfuture_field = "keep"\n')
    raw = {"mine": {"models": ["model"], "future_metadata": {"key": "value"}}, "other": ["else"]}
    (tmp_path / "models-cache.json").write_text(json.dumps(raw))
    assert logins.rename_login("mine", "renamed")
    with (tmp_path / "logins.toml").open("rb") as f:
        assert tomllib.load(f)["renamed"]["future_field"] == "keep"
    assert json.loads((tmp_path / "models-cache.json").read_text()) == {
        "renamed": raw["mine"], "other": raw["other"],
    }


def test_rename_cache_write_failure_restores_login(monkeypatch, tmp_path):
    logins.save_login(logins.Login("mine", provider_api_key="secret"))
    logins.cache_models("mine", ["model"])
    before = (tmp_path / "logins.toml").read_bytes()

    def fail(_):
        raise OSError("disk full")

    monkeypatch.setattr(logins, "_write_raw_model_cache", fail)
    with pytest.raises(OSError, match="disk full"):
        logins.rename_login("mine", "renamed")
    assert (tmp_path / "logins.toml").read_bytes() == before
    assert logins.load_model_cache() == {"mine": ["model"]}


def test_rename_refuses_unreadable_cache_without_changing_login(tmp_path):
    logins.save_login(logins.Login("mine"))
    before = (tmp_path / "logins.toml").read_bytes()
    (tmp_path / "models-cache.json").write_text("{")
    with pytest.raises(ValueError):
        logins.rename_login("mine", "renamed")
    assert (tmp_path / "logins.toml").read_bytes() == before


def test_rename_oauth_provider_refused_without_losing_credentials():
    logins.save_login(logins.Login("openai-codex", codex_refresh_token="refresh"))
    with pytest.raises(ValueError, match="OAuth"):
        logins.rename_login("openai-codex", "renamed")
    assert logins.load_logins()["openai-codex"].codex_refresh_token == "refresh"


def test_manager_rename_and_delete_shortcuts(monkeypatch):
    logins.save_login(logins.Login("mine", sdk_provider_id="openai"))
    logins.cache_models("mine", ["model"])
    screens = iter([Screen([curses.KEY_DOWN, ord("r")]),
                    Screen([curses.KEY_DOWN, ord("d")]), Screen([ord("q")])])
    monkeypatch.setattr(curses, "wrapper", lambda fn, *a, **kw: fn(next(screens), *a, **kw))
    monkeypatch.setattr(login_cli, "_input", lambda *a, **kw: "renamed")
    assert login_cli._run_login() == 0
    assert logins.load_logins() == {}
    assert logins.load_model_cache() == {}


def test_provider_search_returns_original_index():
    screen = Screen(map(ord, "/BETA\n\n"))
    assert login_cli._curses_menu(screen, ["alpha", "beta", "gamma"], "providers") == 1


def test_model_search_toggles_only_matching_models():
    screen = Screen(map(ord, "/beta\nn\n"))
    assert login_cli._curses_multiselect(
        screen, [("alpha", ""), ("beta", ""), ("gamma", "")], "models", preselected={0, 1, 2},
    ) == [0, 2]


def test_search_no_matches_does_not_select_wrong_provider():
    screen = Screen([*map(ord, "/missing\n\n"), 27])
    assert login_cli._curses_menu(screen, ["alpha"], "providers") is None


def test_add_custom_is_first_and_selectable(monkeypatch):
    monkeypatch.setattr(curses, "wrapper", lambda fn, *a, **kw: fn(Screen([10]), *a, **kw))
    assert login_cli._select_provider() == "__custom__"


def test_manager_displays_saved_details_with_masked_credentials(monkeypatch):
    monkeypatch.setenv("JS_PARANOID_MODE", "on")
    logins.save_login(logins.Login("mine", provider_base_url="http://localhost:8000/v1", provider_api_key="private"))
    logins.cache_models("mine", [f"model-{i}" for i in range(37)])
    screen = Screen([curses.KEY_DOWN, ord("q")])
    monkeypatch.setattr(curses, "wrapper", lambda fn, *a, **kw: fn(screen, *a, **kw))
    assert login_cli._run_login() == 0
    output = "\n".join(screen.rendered)
    assert "http://localhost:8000/v1" in output
    assert "private" not in output
    assert re.search(r"\b37\b", output)  # displayed cache count, independent of wording
    assert "saved" in output
    assert "<add custom provider>" in output


def test_saved_update_is_offline_and_preserves_oauth_fields(monkeypatch):
    logins.save_login(logins.Login("mine", provider_api_key="old", codex_refresh_token="refresh"))
    answers = iter(["http://localhost:9000/v1", "new", "Authorization=Bearer token"])
    monkeypatch.setattr(login_cli, "_input", lambda *a, **kw: next(answers))
    monkeypatch.setattr(login_cli, "test_login_with_metadata", lambda *_: pytest.fail("unexpected fetch"))
    monkeypatch.setattr(login_cli, "_run_secondary_test", lambda *_: pytest.fail("unexpected test"))
    login_cli._edit_saved_provider(logins.load_logins()["mine"])
    saved = logins.load_logins()["mine"]
    assert saved.provider_base_url == "http://localhost:9000/v1"
    assert saved.provider_api_key == "new"
    assert saved.provider_headers == {"Authorization": "Bearer token"}
    assert saved.codex_refresh_token == "refresh"


def test_cancel_update_leaves_store_untouched(monkeypatch, tmp_path):
    logins.save_login(logins.Login("mine", provider_api_key="old"))
    before = (tmp_path / "logins.toml").read_bytes()
    answers = iter(["http://changed", None])
    monkeypatch.setattr(login_cli, "_input", lambda *a, **kw: next(answers))
    login_cli._edit_saved_provider(logins.load_logins()["mine"])
    assert (tmp_path / "logins.toml").read_bytes() == before


def test_manager_removes_provider_and_cache(monkeypatch):
    logins.save_login(logins.Login("mine"))
    logins.cache_models("mine", ["model"])
    screens = iter([Screen([curses.KEY_DOWN, 10]), Screen([curses.KEY_END, 10]), Screen([ord("q")])])
    monkeypatch.setattr(curses, "wrapper", lambda fn, *a, **kw: fn(next(screens), *a, **kw))
    assert login_cli._run_login() == 0
    assert logins.load_logins() == {}
    assert logins.load_model_cache() == {}


def test_models_add_works_with_empty_cache_and_deduplicates(monkeypatch):
    login = logins.Login("mine")
    screens = iter([Screen([curses.KEY_DOWN, 10]), Screen([ord("q")])])
    monkeypatch.setattr(curses, "wrapper", lambda fn, *a, **kw: fn(next(screens), *a, **kw))
    monkeypatch.setattr(login_cli, "_input", lambda *a, **kw: " first, first, second, ")
    login_cli._manage_models(login)
    assert logins.load_model_cache()["mine"] == ["first", "second"]


def test_models_refetch_curates_live_list_and_preserves_metadata(monkeypatch):
    login = logins.Login("mine")
    logins.cache_models("mine", ["old"])
    metadata = {"new": logins.ModelCacheMetadata(context_window=12345)}
    monkeypatch.setattr(login_cli, "test_login_with_metadata", lambda _: (["new", "other"], metadata))
    monkeypatch.setattr(login_cli, "_select_models_to_cache", lambda *a, **kw: ["new"])
    screens = iter([Screen([curses.KEY_DOWN, curses.KEY_DOWN, 10]), Screen([ord("q")])])
    monkeypatch.setattr(curses, "wrapper", lambda fn, *a, **kw: fn(next(screens), *a, **kw))
    login_cli._manage_models(login)
    assert logins.load_model_cache()["mine"] == ["new"]
    assert logins.load_model_cache_metadata()["mine"] == metadata


def test_models_refetch_failure_leaves_cache_unchanged(monkeypatch):
    login = logins.Login("mine")
    logins.cache_models("mine", ["old"])
    def fail(_):
        raise RuntimeError("offline")
    monkeypatch.setattr(login_cli, "test_login_with_metadata", fail)
    screens = iter([Screen([curses.KEY_DOWN, curses.KEY_DOWN, 10]), Screen([ord("q")])])
    monkeypatch.setattr(curses, "wrapper", lambda fn, *a, **kw: fn(next(screens), *a, **kw))
    login_cli._manage_models(login)
    assert logins.load_model_cache()["mine"] == ["old"]


def test_manager_model_edit_search_persists_choices(monkeypatch):
    login = logins.Login("mine")
    logins.cache_models("mine", ["alpha", "beta", "gamma"])
    screens = iter([Screen([10]), Screen(map(ord, "/beta\n \n")), Screen([ord("q")])])
    monkeypatch.setattr(login_cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(login_cli.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(login_cli, "_input", lambda *a, **kw: "")
    monkeypatch.setattr(curses, "wrapper", lambda fn, *a, **kw: fn(next(screens), *a, **kw))
    login_cli._manage_models(login)
    assert logins.load_model_cache()["mine"] == ["alpha", "gamma"]


def test_search_ignores_navigation_keys_while_typing():
    screen = Screen([ord("/"), curses.KEY_DOWN, *map(ord, "beta\n\n")])
    assert login_cli._curses_menu(screen, ["alpha", "beta"], "providers") == 1


def test_update_enter_keeps_secrets_and_dash_clears(monkeypatch):
    logins.save_login(logins.Login("mine", provider_api_key="old", provider_headers={"Secret": "token"}))
    answers = iter(["", "", "", "-", "-", "-"])
    monkeypatch.setattr(login_cli, "_input", lambda *a, **kw: next(answers))
    login_cli._edit_saved_provider(logins.load_logins()["mine"])
    saved = logins.load_logins()["mine"]
    assert saved.provider_api_key == "old"
    assert saved.provider_headers == {"Secret": "token"}
    login_cli._edit_saved_provider(saved)
    saved = logins.load_logins()["mine"]
    assert saved.provider_base_url is None
    assert saved.provider_api_key is None
    assert saved.provider_headers == {}


def test_model_refetch_cancel_preserves_cache_bytes(monkeypatch, tmp_path):
    logins.cache_models("mine", ["old"])
    before = (tmp_path / "models-cache.json").read_bytes()
    monkeypatch.setattr(login_cli, "test_login_with_metadata", lambda _: (["new"], {}))
    monkeypatch.setattr(login_cli, "_select_models_to_cache", lambda *a, **kw: None)
    screens = iter([Screen([curses.KEY_DOWN, curses.KEY_DOWN, 10]), Screen([ord("q")])])
    monkeypatch.setattr(curses, "wrapper", lambda fn, *a, **kw: fn(next(screens), *a, **kw))
    login_cli._manage_models(logins.Login("mine"))
    assert (tmp_path / "models-cache.json").read_bytes() == before


def test_deselect_all_keeps_list_models_empty_without_network(monkeypatch, capsys):
    from js import cli

    logins.save_login(logins.Login("mine", sdk_provider_id="openai"))
    logins.cache_models("mine", ["old"])
    monkeypatch.setattr(login_cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(login_cli.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(login_cli, "_input", lambda *a, **kw: "")
    monkeypatch.setattr(curses, "wrapper", lambda fn, *a, **kw: fn(Screen(map(ord, "n\n")), *a, **kw))
    assert login_cli._run_models_edit("mine") == 0
    capsys.readouterr()
    monkeypatch.setattr(logins, "test_login", lambda *_: pytest.fail("empty curated cache must not fetch"))
    assert cli._print_model_list("mine", None) == 0
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == ""
