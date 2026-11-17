"""$NAME in scripts, jsrc lines, alias bodies and event handlers: expanded
once, never in a typed line and never again in what it put in."""

from __future__ import annotations

import contextlib
import copy
import io

from repl_driver import record_turns, turn_stub, type_into_js

from js import settings
from js.repl_state import ReplState


def test_dollar_names_expand_once_in_aliases_handlers_and_scripts(tmp_path, monkeypatch):
    from js import cli

    monkeypatch.chdir(tmp_path)
    seen: list[dict] = []

    def turn(cfg, system, messages, *args, event_hooks, **kwargs):
        # The live settings as the turn starts, then the turn's turn_start.
        seen.append(copy.deepcopy(cfg.settings))
        event_hooks.emit("turn_start")

    monkeypatch.setattr(cli.runtime, "run_turn_async", turn_stub(turn))
    script = tmp_path / "boot.irc"
    script.write_text("set BANNER [$MODEL]\n", encoding="utf-8")

    assert type_into_js(monkeypatch, [
        "/set PROVIDER openai",
        "/alias pin set MODEL $PROVIDER",
        "/pin",
        f"/load {script}",
        "/on turn_start set BANNER $BANNER$BANNER",
        "one",
        "two",
    ]) == 0

    first, second = seen
    assert settings.knob(first, "MODEL") == "openai"
    assert settings.knob(first, "BANNER") == "[openai]"
    assert settings.knob(second, "BANNER") == "[openai][openai]"


def test_a_typed_line_and_an_expanded_value_are_not_expanded(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    turns = record_turns(monkeypatch)

    assert type_into_js(monkeypatch, [
        "/set BANNER $MODEL",
        "one",
        "/alias again set BANNER <$BANNER>",
        "/again",
        "two",
    ]) == 0

    first, second = turns
    assert settings.knob(first.settings, "BANNER") == "$MODEL"
    assert settings.knob(second.settings, "BANNER") == "<$MODEL>"


def test_dollar_names_expand_in_a_user_jsrc(tmp_path):
    path = tmp_path / "jsrc"
    path.write_text("set PROVIDER openai\nset BANNER $PROVIDER:\n", encoding="utf-8")
    loaded = settings.seed_defaults()
    assert settings.load_jsrc_files([path], loaded) == []
    assert settings.knob(loaded, "BANNER") == "openai:"


def test_a_literal_dollar_survives_save_and_reload(tmp_path):
    live = settings.seed_defaults()
    settings.set_dotted(live, ("BANNER",), "$MODEL costs $$5")
    path = tmp_path / "jsrc"
    settings.save_settings_to_jsrc(path, live, keys=["BANNER"])
    loaded = settings.seed_defaults()
    assert settings.load_jsrc_files([path], loaded) == []
    assert settings.knob(loaded, "BANNER") == "$MODEL costs $$5"


def test_a_display_setting_line_expands_like_set_and_a_saved_dollar_reloads(tmp_path):
    path = tmp_path / "jsrc"
    path.write_text("set REASONING_DISPLAY 3\nset TOOLS_DISPLAY $REASONING_DISPLAY\n", encoding="utf-8")
    loaded = settings.seed_defaults()
    assert settings.load_jsrc_files([path], loaded) == []
    assert settings.knob(loaded, "TOOLS_DISPLAY") == 3

    live = settings.seed_defaults()
    settings.set_dotted(live, ("SHELL_QUIET",), ["echo $HOME"])
    saved = tmp_path / "saved"
    settings.save_settings_to_jsrc(saved, live, keys=["SHELL_QUIET"])
    again = settings.seed_defaults()
    assert settings.load_jsrc_files([saved], again) == []
    assert settings.knob(again, "SHELL_QUIET") == ["echo $HOME"]


def test_a_format_line_keeps_its_dollar_names_for_when_it_is_drawn(tmp_path):
    from js import fset

    path = tmp_path / "jsrc"
    path.write_text("fset MODEL_SET %G now $$0 $0\n", encoding="utf-8")
    assert settings.load_jsrc_files([path], settings.seed_defaults()) == []
    assert fset.lookup("MODEL_SET") == "%G now $$0 $0"


def _replay(tmp_path, jsrc):
    from types import SimpleNamespace

    from js import cli

    loaded = settings.seed_defaults()
    assert settings.load_jsrc_files([jsrc], loaded) == []
    state = ReplState(messages=[], system="s", settings=loaded, aliases={})
    assert cli._run_rc_commands(state, SimpleNamespace(settings=loaded, project_dir=tmp_path), [jsrc]) == []
    return state


def test_a_replayed_alias_loads_relative_to_its_own_jsrc(tmp_path):
    home = tmp_path / "conf"
    home.mkdir()
    (home / "child.irc").write_text("alias kid set BANNER kid\n", encoding="utf-8")
    jsrc = home / "jsrc"
    jsrc.write_text("alias boot load child.irc\nboot\n", encoding="utf-8")
    elsewhere = tmp_path / "project"
    elsewhere.mkdir()
    state = _replay(elsewhere, jsrc)
    assert state.aliases.get("kid") == "set BANNER kid"


def _startup_effects(root):
    from types import SimpleNamespace

    from js import cli

    jsrc = root / "jsrc"
    jsrc.write_text("load f0\n", encoding="utf-8")
    loaded = settings.seed_defaults()
    settings.load_jsrc_files([jsrc], loaded)
    state = ReplState(messages=[], system="s", settings=loaded, aliases={})
    cli._run_rc_commands(state, SimpleNamespace(settings=loaded, project_dir=root), [jsrc])
    live = state.settings
    return (settings.knob(live, "HISTORY_SIZE"), settings.knob(live, "EDITING_MODE"), state.aliases)


def _load_effects(root):
    from types import SimpleNamespace

    from js import cli

    live = settings.seed_defaults()
    state = ReplState(messages=[], system="s", settings=live, aliases={})
    cfg = SimpleNamespace(settings=live, project_dir=root)
    with contextlib.redirect_stdout(io.StringIO()):
        cli._cmd_load(str(root / "f0"), state, cfg)
    return (settings.knob(live, "HISTORY_SIZE"), settings.knob(live, "EDITING_MODE"), state.aliases)


def test_startup_runs_an_alias_where_it_stands_among_the_settings(tmp_path):
    (tmp_path / "f0").write_text("set HISTORY_SIZE 10\nalias bump set HISTORY_SIZE 20\nbump\n"
                                 "set HISTORY_SIZE 30\n", encoding="utf-8")
    assert _startup_effects(tmp_path)[0] == _load_effects(tmp_path)[0] == 30
