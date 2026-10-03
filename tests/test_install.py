"""What `just install` asks (js.install): the keys missing from the
environment and ~/.js/.env, and a default model when the one js starts with
has no provider to run on. Every test runs in the tmp HOME the conftest
installs; answers are scripted."""

from __future__ import annotations

import stat

import pytest

from js import dotenv, install, logins, paths, settings


def _answers(*replies):
    """An ask that returns `replies` in order and records each prompt."""
    asked: list[str] = []
    pending = list(replies)

    def ask(prompt):
        asked.append(prompt)
        return pending.pop(0)

    ask.asked = asked
    return ask


def _names(missing):
    return [name for name, _use in missing]


def _write_jsrc(text: str) -> None:
    path = paths.global_config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _login(provider_id: str, models: list[str]) -> None:
    logins.save_login(logins.Login(provider_id=provider_id, provider_api_key="k"))
    logins.cache_models(provider_id, models)


# --- keys -------------------------------------------------------------------------


def test_a_key_in_the_environment_or_the_env_file_is_not_missing():
    env_file = paths.global_env_file()
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text("EXA_API_KEY=from-file\nSERPER_API_KEY=\n", encoding="utf-8")

    missing = install.missing_keys({"TAVILY_API_KEY": "from-env"}, env_file)

    assert _names(missing) == ["TYPESAFE_API_KEY", "SERPER_API_KEY", "CONTEXT7_API_KEY"]


def test_answers_are_saved_mode_600_and_enter_skips():
    env_file = paths.global_env_file()
    ask = _answers("ts-key", "", "  exa-key  ", "", "")

    saved = install.ask_keys({}, env_file, ask)

    assert saved == ["TYPESAFE_API_KEY", "EXA_API_KEY"]
    assert len(ask.asked) == len(install.KEYS)
    assert dotenv.parse(env_file.read_text(encoding="utf-8")) == {
        "TYPESAFE_API_KEY": "ts-key", "EXA_API_KEY": "exa-key"}
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_a_rerun_asks_only_for_the_keys_still_missing():
    env_file = paths.global_env_file()
    install.ask_keys({}, env_file, _answers("ts-key", "", "exa-key", "", ""))
    ask = _answers("tavily-key", "", "")

    install.ask_keys({}, env_file, ask)

    named = [[name for name, _use in install.KEYS if name in prompt] for prompt in ask.asked]
    assert named == [["TAVILY_API_KEY"], ["SERPER_API_KEY"], ["CONTEXT7_API_KEY"]]
    assert _names(install.missing_keys({}, env_file)) == ["SERPER_API_KEY", "CONTEXT7_API_KEY"]


def test_a_closed_terminal_stops_the_questions():
    ask = _answers(None)
    assert install.ask_keys({}, paths.global_env_file(), ask) == []
    assert len(ask.asked) == 1


def test_a_key_joins_an_existing_env_file_and_tightens_its_mode():
    env_file = paths.global_env_file()
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text("OTHER=1", encoding="utf-8")
    env_file.chmod(0o644)

    install.save_key(env_file, "EXA_API_KEY", "a b#c")

    assert dotenv.parse(env_file.read_text(encoding="utf-8")) == {"OTHER": "1", "EXA_API_KEY": "a b#c"}
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


# --- the default model -------------------------------------------------------------


def test_a_model_whose_provider_has_a_saved_login_routes():
    _write_jsrc("set model.id deepseek/deepseek-v4-flash\n")
    assert install.default_model({}) == ("deepseek/deepseek-v4-flash", False)
    _login("deepseek", ["deepseek-v4-flash"])
    assert install.default_model({}) == ("deepseek/deepseek-v4-flash", True)


def test_a_routed_default_model_asks_nothing():
    _write_jsrc("set model.id deepseek/deepseek-v4-flash\n")
    _login("deepseek", ["deepseek-v4-flash"])
    ask = _answers()

    assert install.ask_model({}, ask, pick=pytest.fail, add_provider=pytest.fail) is None
    assert ask.asked == []


def test_a_picked_model_becomes_model_id_in_the_jsrc_and_the_rest_stays():
    _write_jsrc("# mine\nset model.id deepseek/deepseek-v4-flash\nset ui.net 2\nalias x /help\n")
    _login("mimo", ["mimo-v2", "mimo-v3"])

    chosen = install.ask_model({}, _answers("p"), pick=lambda: {"provider_id": "mimo", "model": "mimo-v3"},
                               add_provider=pytest.fail)

    assert chosen == "mimo/mimo-v3"
    assert paths.global_config_file().read_text(encoding="utf-8") == (
        "# mine\nset model.id mimo/mimo-v3\nset ui.net 2\nalias x /help\n")
    assert install.default_model({}) == ("mimo/mimo-v3", True)


def test_with_no_saved_login_the_login_flow_adds_one_then_a_model_is_picked():
    _write_jsrc("set model.id deepseek/deepseek-v4-flash\n")
    ask = _answers("a", "p")

    chosen = install.ask_model({}, ask, pick=lambda: {"provider_id": "mimo", "model": "mimo-v3"},
                               add_provider=lambda: _login("mimo", ["mimo-v3"]))

    assert chosen == "mimo/mimo-v3"
    assert len(ask.asked) == 2 and ask.asked[0] != ask.asked[1]


def test_enter_or_a_cancelled_pick_leaves_the_jsrc_alone():
    text = "set model.id deepseek/deepseek-v4-flash\n"
    _write_jsrc(text)
    _login("mimo", ["mimo-v3"])

    assert install.ask_model({}, _answers("p", ""), pick=lambda: None, add_provider=pytest.fail) is None
    assert paths.global_config_file().read_text(encoding="utf-8") == text


def test_write_model_id_replaces_an_unset_line_and_appends_when_there_is_none(tmp_path):
    jsrc = tmp_path / "jsrc"
    jsrc.write_text("set ui.net 2\nset -model.id\n", encoding="utf-8")
    settings.write_model_id(jsrc, "mimo/mimo-v3")
    assert jsrc.read_text(encoding="utf-8") == "set ui.net 2\nset model.id mimo/mimo-v3\n"

    bare = tmp_path / "bare"
    bare.write_text("set ui.net 2", encoding="utf-8")
    settings.write_model_id(bare, "mimo/mimo-v3")
    assert bare.read_text(encoding="utf-8") == "set ui.net 2\nset model.id mimo/mimo-v3\n"


# --- without a terminal -------------------------------------------------------------


def test_without_a_terminal_it_names_what_is_missing_and_asks_nothing(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr(install, "getpass", pytest.fail)

    assert install.main([], environ={"TAVILY_API_KEY": "x"}) == 0

    out = capsys.readouterr().out
    assert "TYPESAFE_API_KEY" in out and "TAVILY_API_KEY" not in out
    assert paths.global_config_file().is_file()
    assert not paths.global_env_file().exists()
