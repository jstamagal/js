"""Provider management uses real stores and headless terminal input."""
import curses
import re
import sys

import pytest

from js import cli, curses_menu, listings, login_cli, logins


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
    logins.set_config_dir(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(curses, "curs_set", lambda _: None)


def at_a_terminal(monkeypatch) -> None:
    """Pickers refuse without a terminal; these tests drive them through a
    fake curses screen, so stdin and stdout stand in for one."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)


def screens(monkeypatch, *keys) -> list[str]:
    """Each curses screen js opens reads the next of `keys`; the titles of
    the screens opened are returned."""
    pending = iter(keys)
    titles: list[str] = []

    def wrapper(fn, *args, **kwargs):
        titles.append(args[1])
        return fn(Screen(next(pending)), *args, **kwargs)

    monkeypatch.setattr(curses, "wrapper", wrapper)
    return titles


def js_login(monkeypatch) -> int:
    """`js --login` with no provider, at a terminal: the provider manager."""
    at_a_terminal(monkeypatch)
    return cli.main(["--login"])


# The manager's keys that open its only saved provider, the row after
# <add custom provider>; the saved provider's keys for Update and Models;
# the models screen's keys for Select, Add and Re-fetch; q leaves a screen.
OPEN_SAVED = [curses.KEY_DOWN, 10]
UPDATE = [10]
MODELS = [curses.KEY_DOWN, 10]
SELECT = [10]
ADD = [curses.KEY_DOWN, 10]
REFETCH = [curses.KEY_DOWN, curses.KEY_DOWN, 10]
QUIT = [ord("q")]


def test_registry_picker_accepts_direct_typeahead(monkeypatch):
    # The manager's last row opens the registry picker; typing "de" picks deepseek.
    fetched = []
    monkeypatch.setattr(login_cli, "_input", lambda prompt, *, default=None, secret=False: default or "sk")
    monkeypatch.setattr(login_cli, "test_login_with_metadata",
                        lambda login: fetched.append(login.provider_id) or ([], {}))
    screens(monkeypatch, [curses.KEY_END, 10], map(ord, "de\n"), QUIT)
    assert js_login(monkeypatch) == 0
    assert fetched == ["deepseek"]


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
    from js import messages as msgs

    monkeypatch.setattr(login_cli, "_input", lambda *a, **kw: "'cpa")
    opened = screens(monkeypatch, [10], QUIT)
    assert js_login(monkeypatch) == 0
    assert msgs.LOGIN_PICK_SHAPE.text() not in opened, "invalid name reached API shape selection"
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


def test_manager_rename_and_delete_shortcuts(monkeypatch):
    logins.save_login(logins.Login("mine", sdk_provider_id="openai"))
    logins.cache_models("mine", ["model"])
    screens = iter([Screen([curses.KEY_DOWN, ord("r")]),
                    Screen([curses.KEY_DOWN, ord("d")]), Screen([ord("q")])])
    monkeypatch.setattr(curses, "wrapper", lambda fn, *a, **kw: fn(next(screens), *a, **kw))
    monkeypatch.setattr(login_cli, "_input", lambda *a, **kw: "renamed")
    assert js_login(monkeypatch) == 0
    assert logins.load_logins() == {}
    assert logins.load_model_cache() == {}


def test_provider_search_returns_original_index():
    screen = Screen(map(ord, "/BETA\n\n"))
    assert curses_menu.menu(screen, ["alpha", "beta", "gamma"], "providers") == 1


def test_model_search_toggles_only_matching_models():
    screen = Screen(map(ord, "/beta\nn\n"))
    assert curses_menu.multiselect(
        screen, [("alpha", ""), ("beta", ""), ("gamma", "")], "models", preselected={0, 1, 2},
    ) == [0, 2]


def test_search_no_matches_does_not_select_wrong_provider():
    screen = Screen([*map(ord, "/missing\n\n"), 27])
    assert curses_menu.menu(screen, ["alpha"], "providers") is None


def test_add_custom_is_first_and_selectable(monkeypatch):
    from js import messages as msgs

    # The manager's last row opens the registry picker; Enter on its first row
    # adds a custom provider, which asks for its id first.
    asked = []
    monkeypatch.setattr(login_cli, "_input", lambda prompt, **kw: asked.append(prompt))
    screens(monkeypatch, [curses.KEY_END, 10], [10], QUIT)
    assert js_login(monkeypatch) == 0
    assert asked == [msgs.LOGIN_ASK_CUSTOM_ID.text()]


def test_manager_displays_saved_details_with_masked_credentials(monkeypatch):
    monkeypatch.setenv("JS_PARANOID_MODE", "on")
    logins.save_login(logins.Login("mine", provider_base_url="http://localhost:8000/v1", provider_api_key="private"))
    logins.cache_models("mine", [f"model-{i}" for i in range(37)])
    screen = Screen([curses.KEY_DOWN, ord("q")])
    monkeypatch.setattr(curses, "wrapper", lambda fn, *a, **kw: fn(screen, *a, **kw))
    assert js_login(monkeypatch) == 0
    output = "\n".join(screen.rendered)
    assert "http://localhost:8000/v1" in output
    assert "private" not in output
    assert re.search(r"\b37\b", output)  # displayed cache count, independent of wording
    assert "saved" in output
    assert "<add custom provider>" in output


def test_saved_update_is_offline(monkeypatch):
    logins.save_login(logins.Login("mine", provider_api_key="old"))
    answers = iter(["http://localhost:9000/v1", "new", "Authorization=Bearer token"])
    monkeypatch.setattr(login_cli, "_input", lambda *a, **kw: next(answers))
    monkeypatch.setattr(login_cli, "test_login_with_metadata", lambda *_: pytest.fail("unexpected fetch"))
    monkeypatch.setattr(login_cli.model_client, "stream_model", lambda **_: pytest.fail("unexpected test"))
    screens(monkeypatch, OPEN_SAVED, UPDATE, QUIT, QUIT)
    assert js_login(monkeypatch) == 0
    saved = logins.load_logins()["mine"]
    assert saved.provider_base_url == "http://localhost:9000/v1"
    assert saved.provider_api_key == "new"
    assert saved.provider_headers == {"Authorization": "Bearer token"}


def test_cancel_update_leaves_store_untouched(monkeypatch, tmp_path):
    logins.save_login(logins.Login("mine", provider_api_key="old"))
    before = (tmp_path / "logins.toml").read_bytes()
    answers = iter(["http://changed", None])
    monkeypatch.setattr(login_cli, "_input", lambda *a, **kw: next(answers))
    screens(monkeypatch, OPEN_SAVED, UPDATE, QUIT, QUIT)
    assert js_login(monkeypatch) == 0
    assert (tmp_path / "logins.toml").read_bytes() == before


def test_manager_removes_provider_and_cache(monkeypatch):
    logins.save_login(logins.Login("mine"))
    logins.cache_models("mine", ["model"])
    screens = iter([Screen([curses.KEY_DOWN, 10]), Screen([curses.KEY_END, 10]), Screen([ord("q")])])
    monkeypatch.setattr(curses, "wrapper", lambda fn, *a, **kw: fn(next(screens), *a, **kw))
    assert js_login(monkeypatch) == 0
    assert logins.load_logins() == {}
    assert logins.load_model_cache() == {}


def test_models_add_works_with_empty_cache_and_deduplicates(monkeypatch):
    logins.save_login(logins.Login("mine"))
    screens(monkeypatch, OPEN_SAVED, MODELS, ADD, QUIT, QUIT, QUIT)
    monkeypatch.setattr(login_cli, "_input", lambda *a, **kw: " first, first, second, ")
    assert js_login(monkeypatch) == 0
    assert logins.load_model_cache()["mine"] == ["first", "second"]


def test_models_refetch_curates_live_list_and_preserves_metadata(monkeypatch):
    logins.save_login(logins.Login("mine"))
    logins.cache_models("mine", ["old"])
    metadata = {"new": logins.ModelCacheMetadata(context_window=12345)}
    monkeypatch.setattr(login_cli, "test_login_with_metadata", lambda _: (["new", "other"], metadata))
    monkeypatch.setattr(login_cli, "_input", lambda *a, **kw: "")
    # The keep-list shows new, other (both kept) and old: drop other.
    keep_new = [curses.KEY_DOWN, ord(" "), 10]
    screens(monkeypatch, OPEN_SAVED, MODELS, REFETCH, keep_new, QUIT, QUIT, QUIT)
    assert js_login(monkeypatch) == 0
    assert logins.load_model_cache()["mine"] == ["new"]
    assert logins.load_model_cache_metadata()["mine"] == metadata


def test_models_refetch_failure_leaves_cache_unchanged(monkeypatch):
    logins.save_login(logins.Login("mine"))
    logins.cache_models("mine", ["old"])
    def fail(_):
        raise RuntimeError("offline")
    monkeypatch.setattr(login_cli, "test_login_with_metadata", fail)
    screens(monkeypatch, OPEN_SAVED, MODELS, REFETCH, QUIT, QUIT, QUIT)
    assert js_login(monkeypatch) == 0
    assert logins.load_model_cache()["mine"] == ["old"]


def test_manager_model_edit_search_persists_choices(monkeypatch):
    logins.save_login(logins.Login("mine"))
    logins.cache_models("mine", ["alpha", "beta", "gamma"])
    monkeypatch.setattr(login_cli, "_input", lambda *a, **kw: "")
    screens(monkeypatch, OPEN_SAVED, MODELS, SELECT, map(ord, "/beta\n \n"), QUIT, QUIT, QUIT)
    assert js_login(monkeypatch) == 0
    assert logins.load_model_cache()["mine"] == ["alpha", "gamma"]


def test_search_ignores_navigation_keys_while_typing():
    screen = Screen([ord("/"), curses.KEY_DOWN, *map(ord, "beta\n\n")])
    assert curses_menu.menu(screen, ["alpha", "beta"], "providers") == 1


def test_update_enter_keeps_secrets_and_dash_clears(monkeypatch):
    logins.save_login(logins.Login("mine", provider_api_key="old", provider_headers={"Secret": "token"}))
    answers = iter(["", "", "", "-", "-", "-"])
    monkeypatch.setattr(login_cli, "_input", lambda *a, **kw: next(answers))
    screens(monkeypatch, OPEN_SAVED, UPDATE, QUIT, QUIT)
    assert js_login(monkeypatch) == 0
    saved = logins.load_logins()["mine"]
    assert saved.provider_api_key == "old"
    assert saved.provider_headers == {"Secret": "token"}
    screens(monkeypatch, OPEN_SAVED, UPDATE, QUIT, QUIT)
    assert js_login(monkeypatch) == 0
    saved = logins.load_logins()["mine"]
    assert saved.provider_base_url is None
    assert saved.provider_api_key is None
    assert saved.provider_headers == {}


def test_model_refetch_cancel_preserves_cache_bytes(monkeypatch, tmp_path):
    logins.save_login(logins.Login("mine"))
    logins.cache_models("mine", ["old"])
    before = (tmp_path / "models-cache.json").read_bytes()
    monkeypatch.setattr(login_cli, "test_login_with_metadata", lambda _: (["new"], {}))
    screens(monkeypatch, OPEN_SAVED, MODELS, REFETCH, QUIT, QUIT, QUIT, QUIT)
    assert js_login(monkeypatch) == 0
    assert (tmp_path / "models-cache.json").read_bytes() == before


def test_deselect_all_keeps_list_models_empty_without_network(monkeypatch, capsys):

    logins.save_login(logins.Login("mine", sdk_provider_id="openai"))
    logins.cache_models("mine", ["old"])
    monkeypatch.setattr(login_cli, "_input", lambda *a, **kw: "")
    monkeypatch.setattr(curses, "wrapper", lambda fn, *a, **kw: fn(Screen(map(ord, "n\n")), *a, **kw))
    at_a_terminal(monkeypatch)
    assert cli.main(["--models-edit", "mine"]) == 0
    capsys.readouterr()
    monkeypatch.setattr(logins, "test_login", lambda *_: pytest.fail("empty curated cache must not fetch"))
    assert listings.print_model_list("mine", None) == 0
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == ""
