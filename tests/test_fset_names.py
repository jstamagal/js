"""$NAME in scripts, jsrc lines, alias bodies and event handlers: expanded
once, never in a typed line and never again in what it put in."""

from __future__ import annotations

import contextlib
import io

from js import settings


def _repl_state():
    return {"messages": [], "system": "sys", "settings": settings.seed_defaults(), "aliases": {}}


def test_dollar_names_expand_once_in_aliases_handlers_and_scripts(tmp_path):
    from types import SimpleNamespace

    from js import cli, events

    state = _repl_state()
    cfg = SimpleNamespace(settings=None, project_dir=tmp_path)
    cli._handle_command("/set PROVIDER openai", state, cfg)
    cli._handle_command("/alias pin set MODEL $PROVIDER", state, cfg)
    cli._handle_command("/pin", state, cfg)
    assert settings.knob(state["settings"], "MODEL") == "openai"

    script = tmp_path / "boot.irc"
    script.write_text("set BANNER [$MODEL]\n", encoding="utf-8")
    cli._handle_command(f"/load {script}", state, cfg)
    assert settings.knob(state["settings"], "BANNER") == "[openai]"

    state["events"] = events.EventHooks()
    state["events"].set_dispatcher(cli._event_dispatcher(state, cfg))
    cli._handle_command("/on turn_start set BANNER $BANNER$BANNER", state, cfg)
    state["events"].emit("turn_start")
    assert settings.knob(state["settings"], "BANNER") == "[openai][openai]"


def test_a_typed_line_and_an_expanded_value_are_not_expanded():
    state = _repl_state()
    from types import SimpleNamespace

    from js import cli

    cfg = SimpleNamespace(settings=None)
    cli._handle_command("/set BANNER $MODEL", state, cfg)
    assert settings.knob(state["settings"], "BANNER") == "$MODEL"
    cli._handle_command("/alias again set BANNER <$BANNER>", state, cfg)
    cli._handle_command("/again", state, cfg)
    assert settings.knob(state["settings"], "BANNER") == "<$MODEL>"


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
    settings.save_settings_to_jsrc(path, live)
    loaded = settings.seed_defaults()
    assert settings.load_jsrc_files([path], loaded) == []
    assert settings.knob(loaded, "BANNER") == "$MODEL costs $$5"


def test_a_display_setting_line_expands_like_set_and_a_saved_dollar_reloads(tmp_path):
    path = tmp_path / "jsrc"
    path.write_text("fset REASONING_DISPLAY 3\nfset TOOLS_DISPLAY $REASONING_DISPLAY\n", encoding="utf-8")
    loaded = settings.seed_defaults()
    assert settings.load_jsrc_files([path], loaded) == []
    assert settings.knob(loaded, "TOOLS_DISPLAY") == 3

    live = settings.seed_defaults()
    settings.set_dotted(live, ("SHELL_QUIET",), ["echo $HOME"])
    saved = tmp_path / "saved"
    settings.save_settings_to_jsrc(saved, live)
    again = settings.seed_defaults()
    assert settings.load_jsrc_files([saved], again) == []
    assert settings.knob(again, "SHELL_QUIET") == ["echo $HOME"]


def test_a_format_line_keeps_its_dollar_names_for_when_it_is_drawn(tmp_path):
    from js import fset

    path = tmp_path / "jsrc"
    path.write_text("fset MODEL_SET $BANNER now %s\n", encoding="utf-8")
    assert settings.load_jsrc_files([path], settings.seed_defaults()) == []
    assert fset.lookup("MODEL_SET") == "$BANNER now %s"


def _replay(tmp_path, jsrc):
    from types import SimpleNamespace

    from js import cli

    loaded = settings.seed_defaults()
    assert settings.load_jsrc_files([jsrc], loaded) == []
    state = {"messages": [], "system": "s", "settings": loaded, "aliases": {}}
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
    assert state["aliases"].get("kid") == "set BANNER kid"


def _startup_effects(root):
    from types import SimpleNamespace

    from js import cli

    jsrc = root / "jsrc"
    jsrc.write_text("load f0\n", encoding="utf-8")
    loaded = settings.seed_defaults()
    settings.load_jsrc_files([jsrc], loaded)
    state = {"messages": [], "system": "s", "settings": loaded, "aliases": {}}
    cli._run_rc_commands(state, SimpleNamespace(settings=loaded, project_dir=root), [jsrc])
    live = state["settings"]
    return (settings.knob(live, "HISTORY_SIZE"), settings.knob(live, "EDITING_MODE"), state["aliases"])


def _load_effects(root):
    from types import SimpleNamespace

    from js import cli

    live = settings.seed_defaults()
    state = {"messages": [], "system": "s", "settings": live, "aliases": {}}
    cfg = SimpleNamespace(settings=live, project_dir=root)
    with contextlib.redirect_stdout(io.StringIO()):
        cli._cmd_load(str(root / "f0"), state, cfg)
    return (settings.knob(live, "HISTORY_SIZE"), settings.knob(live, "EDITING_MODE"), state["aliases"])


def test_startup_runs_an_alias_where_it_stands_among_the_settings(tmp_path):
    (tmp_path / "f0").write_text("set HISTORY_SIZE 10\nalias bump set HISTORY_SIZE 20\nbump\n"
                                 "set HISTORY_SIZE 30\n", encoding="utf-8")
    assert _startup_effects(tmp_path)[0] == _load_effects(tmp_path)[0] == 30
