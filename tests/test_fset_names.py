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


def test_a_jsrc_load_of_a_dollar_name_applies_the_loaded_file(tmp_path):
    child = tmp_path / "child.jsrc"
    child.write_text("set BANNER >>\n", encoding="utf-8")
    path = tmp_path / "jsrc"
    path.write_text(f"set MOTD_FILE {child}\nload $MOTD_FILE\n", encoding="utf-8")
    loaded = settings.seed_defaults()
    assert settings.load_jsrc_files([path], loaded) == []
    assert settings.knob(loaded, "BANNER") == ">>"


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


def test_a_load_of_a_dollar_name_replays_the_file_it_loaded_first(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from js import cli

    first, second = tmp_path / "a.irc", tmp_path / "b.irc"
    first.write_text("set BANNER >>\nalias greet set BANNER aaa\n", encoding="utf-8")
    second.write_text("alias greet set BANNER bbb\n", encoding="utf-8")
    jsrc = tmp_path / "jsrc"
    jsrc.write_text(f"set MOTD_FILE {first}\nload $MOTD_FILE\nset MOTD_FILE {second}\n", encoding="utf-8")
    loaded = settings.seed_defaults()
    assert settings.load_jsrc_files([jsrc], loaded) == []
    state = {"messages": [], "system": "s", "settings": loaded, "aliases": {}}
    assert cli._run_rc_commands(state, SimpleNamespace(settings=loaded, project_dir=tmp_path), [jsrc]) == []
    assert settings.knob(loaded, "BANNER") == ">>"
    assert state["aliases"] == {"greet": "set BANNER aaa"}


def _replay(tmp_path, jsrc):
    from types import SimpleNamespace

    from js import cli

    loaded = settings.seed_defaults()
    assert settings.load_jsrc_files([jsrc], loaded) == []
    state = {"messages": [], "system": "s", "settings": loaded, "aliases": {}}
    assert cli._run_rc_commands(state, SimpleNamespace(settings=loaded, project_dir=tmp_path), [jsrc]) == []
    return state


def test_a_script_loaded_twice_replays_each_load_as_it_ran(tmp_path):
    (tmp_path / "a.irc").write_text("alias one set BANNER a\n", encoding="utf-8")
    (tmp_path / "b.irc").write_text("alias two set BANNER b\n", encoding="utf-8")
    shared = tmp_path / "shared.irc"
    shared.write_text("load $MOTD_FILE\n", encoding="utf-8")
    jsrc = tmp_path / "jsrc"
    jsrc.write_text(f"set MOTD_FILE {tmp_path / 'a.irc'}\nload {shared}\n"
                    f"set MOTD_FILE {tmp_path / 'b.irc'}\nload {shared}\n", encoding="utf-8")
    state = _replay(tmp_path, jsrc)
    assert state["aliases"] == {"one": "set BANNER a", "two": "set BANNER b"}


def test_a_dollar_dollar_filename_is_the_literal_file_at_replay_too(tmp_path):
    (tmp_path / "$MODEL.irc").write_text("alias lit set BANNER literal\n", encoding="utf-8")
    (tmp_path / "demo.irc").write_text("alias wrong set BANNER wrong\n", encoding="utf-8")
    jsrc = tmp_path / "jsrc"
    jsrc.write_text(f"set MODEL demo\nload {tmp_path}/$$MODEL.irc\n", encoding="utf-8")
    state = _replay(tmp_path, jsrc)
    assert state["aliases"] == {"lit": "set BANNER literal"}


def test_a_dollar_name_of_a_stock_format_is_its_default_format(tmp_path):
    from js import messages as msgs

    path = tmp_path / "jsrc"
    path.write_text("set BANNER [$MODEL_SET]\n", encoding="utf-8")
    loaded = settings.seed_defaults()
    assert settings.load_jsrc_files([path], loaded) == []
    assert settings.knob(loaded, "BANNER") == f"[{msgs.MODEL_SET.default_format()}]"


def test_an_unreadable_loaded_file_is_reported_at_replay(tmp_path):
    from types import SimpleNamespace

    from js import cli

    child = tmp_path / "child.irc"
    child.write_bytes(b"alias x set BANNER \xff\xfe\n")
    jsrc = tmp_path / "jsrc"
    jsrc.write_text(f"load {child}\n", encoding="utf-8")
    loaded = settings.seed_defaults()
    settings.load_jsrc_files([jsrc], loaded)
    state = {"messages": [], "system": "s", "settings": loaded, "aliases": {}}
    errors = cli._run_rc_commands(state, SimpleNamespace(settings=loaded, project_dir=tmp_path), [jsrc])
    assert len(errors) == 1 and str(child) in errors[0]


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


def test_replay_stops_a_loaded_file_at_its_first_error_and_goes_on_with_its_parent(tmp_path):
    (tmp_path / "child").write_text("bogus nope\nalias leaked set BANNER x\n", encoding="utf-8")
    jsrc = tmp_path / "jsrc"
    jsrc.write_text("load child\nalias kept set BANNER y\n", encoding="utf-8")
    from types import SimpleNamespace

    from js import cli

    loaded = settings.seed_defaults()
    settings.load_jsrc_files([jsrc], loaded)
    state = {"messages": [], "system": "s", "settings": loaded, "aliases": {}}
    errors = cli._run_rc_commands(state, SimpleNamespace(settings=loaded, project_dir=tmp_path), [jsrc])
    assert len(errors) == 1
    assert "leaked" not in state["aliases"]
    assert state["aliases"].get("kept") == "set BANNER y"


def _random_tree(rng, root):
    """Files f0..f5 under ``root``: each a few lines of good and bad settings,
    aliases, unknown commands and loads of later files (some missing)."""
    for index in range(6):
        lines = []
        for _ in range(rng.randint(0, 8)):
            kind = rng.randrange(9)
            if kind == 0:
                lines.append(f"set HISTORY_SIZE {rng.randint(1, 900)}")
            elif kind == 1:
                lines.append("set HISTORY_SIZE lots")
            elif kind == 2:
                lines.append(f"set EDITING_MODE {rng.choice(['vi', 'emacs'])}")
            elif kind == 3:
                lines.append(f"alias a{rng.randint(0, 9)} set BANNER x{rng.randint(0, 9)}")
            elif kind == 4:
                lines.append("bogus nope")
            elif kind == 5 and index < 5:
                lines.append(f"load f{rng.randint(index + 1, 6)}")
            elif kind == 6:
                lines.append(f"alias bump{rng.randint(0, 2)} set HISTORY_SIZE {rng.randint(1, 900)}")
            elif kind == 7:
                lines.append(f"bump{rng.randint(0, 2)}")
            else:
                lines.append("")
        (root / f"f{index}").write_text("\n".join(lines) + "\n", encoding="utf-8")


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


def test_startup_applies_a_tree_of_loads_exactly_as_load_does(tmp_path):
    import random

    rng = random.Random(15)
    for trial in range(200):
        root = tmp_path / f"t{trial}"
        root.mkdir()
        _random_tree(rng, root)
        assert _startup_effects(root) == _load_effects(root), trial
