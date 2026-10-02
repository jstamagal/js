"""The one command table: /save writes everything resident, a restart replays
it, and a jsrc may hold any command line."""

from __future__ import annotations

import os
from pathlib import Path

from js import cli, paths, settings
from js.config import from_env, jsrc_paths
from repl_driver import repl_state


def _launch(cwd):
    """A REPL launch in `cwd`: the config, the state after replaying the jsrc
    files, and the replay's errors."""
    os.chdir(cwd)
    cfg = from_env()
    state, _spec = repl_state(cfg)
    errors = cli._run_rc_commands(state, cfg, jsrc_paths(Path(cfg.project_dir or cwd)))
    return cfg, state, errors


def _run(lines, state, cfg):
    for line in lines:
        assert cli._handle_command(line, state, cfg) is True


def test_save_then_restart_brings_back_handlers_and_aliases(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    cfg, state, _errors = _launch(tmp_path)
    _run([
        "/on turn_start set compact.auto off",
        "/on ^tool_call set runtime.trace on",
        "/alias ca compact-auto $*",
        "/save",
    ], state, cfg)

    _cfg, state, errors = _launch(tmp_path)

    assert errors == []
    assert [h.handler for h in state["events"].handlers_for("turn_start")] == ["set compact.auto off"]
    assert [(h.handler, h.suppress) for h in state["events"].handlers_for("tool_call")] == [
        ("set runtime.trace on", True)
    ]
    assert state["aliases"] == {"ca": "compact-auto $*"}


def test_save_replaces_the_jsrc_with_every_current_setting(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    jsrc = paths.global_config_file()
    jsrc.parent.mkdir(parents=True, exist_ok=True)
    jsrc.write_text("# old\nset limits.max_read_lines 33\n", encoding="utf-8")
    cfg, state, _errors = _launch(tmp_path)
    _run(["/set compact.auto off", "/save"], state, cfg)

    saved = jsrc.read_text(encoding="utf-8")
    reloaded = settings.collect_settings(config_paths=[jsrc], env={})

    assert "# old" not in saved
    assert settings.get_dotted(reloaded, ("compact", "auto")) is False
    assert settings.get_dotted(reloaded, ("limits", "max_read_lines")) == 33
    assert f"set limits.max_tool_iterations {settings.default_value('limits.max_tool_iterations')}" in saved


def test_jsrc_model_and_provider_lines_take_effect(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    jsrc = paths.global_config_file()
    jsrc.parent.mkdir(parents=True, exist_ok=True)
    jsrc.write_text("/provider deepseek\n/model deepseek-chat\n", encoding="utf-8")

    cfg, state, _errors = _launch(tmp_path)
    turn_cfg = cli._cfg_for_live_state(cfg, state)

    assert (turn_cfg.model, turn_cfg.provider_id) == ("deepseek-chat", "deepseek")


def test_rc_replay_leaves_set_lines_to_the_settings_layer(monkeypatch, tmp_path):
    """Env beats jsrc for settings; replaying the rc at startup must not undo that."""
    monkeypatch.chdir(tmp_path)
    jsrc = paths.global_config_file()
    jsrc.parent.mkdir(parents=True, exist_ok=True)
    jsrc.write_text("set compact.auto off\nalias t turns\n", encoding="utf-8")
    monkeypatch.setenv("JS_COMPACT_AUTO", "on")

    _cfg, state, _errors = _launch(tmp_path)

    assert settings.get_dotted(state["settings"], ("compact", "auto")) is True
    assert state["aliases"] == {"t": "turns"}


def test_jsrc_load_resolves_beside_the_jsrc_and_stays_under_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    jsrc = paths.global_config_file()
    jsrc.parent.mkdir(parents=True, exist_ok=True)
    jsrc.write_text("load extra.irc\n", encoding="utf-8")
    (jsrc.parent / "extra.irc").write_text("set compact.auto off\nalias t turns\n", encoding="utf-8")
    monkeypatch.setenv("JS_COMPACT_AUTO", "on")
    project = tmp_path / "project"
    project.mkdir()

    _cfg, state, errors = _launch(project)

    assert errors == []
    assert state["aliases"] == {"t": "turns"}
    assert settings.get_dotted(state["settings"], ("compact", "auto")) is True


def test_settings_in_a_jsrc_loaded_file_apply_at_config_load(tmp_path):
    jsrc = tmp_path / "jsrc"
    jsrc.write_text("load sub/extra.irc\nload jsrc\n", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "extra.irc").write_text("load ../more.irc\n", encoding="utf-8")
    (tmp_path / "more.irc").write_text("set compact.auto off\n", encoding="utf-8")

    live = settings.collect_settings(config_paths=[jsrc], env={})

    assert settings.get_dotted(live, ("compact", "auto")) is False


def test_rc_errors_name_the_line_and_do_not_stop_startup(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    jsrc = paths.global_config_file()
    jsrc.parent.mkdir(parents=True, exist_ok=True)
    jsrc.write_text("bogus verb\nalias t turns\n", encoding="utf-8")

    _cfg, state, errors = _launch(tmp_path)

    assert len(errors) == 1
    assert f"{jsrc}:1" in errors[0]
    assert state["aliases"] == {"t": "turns"}


def _state() -> dict:
    return {"messages": [], "system": "sys", "settings": settings.seed_defaults()}


def test_alias_runs_its_body_with_arguments(tmp_path):
    state = _state()
    cli._handle_command("/alias ca compact-auto $*", state, None)
    cli._handle_command("/alias trace set runtime.trace", state, None)

    assert cli._handle_command("/ca off", state, None) is True
    assert cli._handle_command("/trace off", state, None) is True

    assert settings.get_dotted(state["settings"], ("compact", "auto")) is False
    assert settings.get_dotted(state["settings"], ("runtime", "trace")) is False


def test_alias_cannot_shadow_a_command_and_can_be_removed():
    state = _state()
    cli._handle_command("/alias set turns", state, None)
    cli._handle_command("/alias t turns", state, None)
    cli._handle_command("/alias -t", state, None)

    assert state["aliases"] == {}


def test_self_referencing_alias_stops():
    state = _state()
    cli._handle_command("/alias loop loop", state, None)

    handled, error = cli._run_command("loop", state, None)

    assert handled is True
    assert error


def test_unknown_slash_word_is_not_a_command():
    """A line like `/usr/bin/python is broken` is prose for the model."""
    assert cli._handle_command("/usr/bin/python is broken", _state(), None) is False


def test_skill_with_a_name_is_a_turn_and_bare_skill_is_a_command():
    """`/skill <name> [request]` becomes a user message; bare `/skill` lists."""
    assert cli._handle_command("/skill grilling check the plan", _state(), None) is False
    assert "skill" in cli.COMMANDS
    assert "tools" in cli.COMMANDS


def test_turn_state_commands_come_from_the_table(monkeypatch):
    command = cli.Command(lambda arg, state, cfg: None, "scrub", "test entry", turn_state=True)
    monkeypatch.setitem(cli.COMMANDS, "scrub", command)

    assert cli._is_turn_state_command("/scrub now")
    assert not cli._is_turn_state_command("/turns")


def test_registering_the_same_handler_twice_keeps_one(tmp_path):
    """A handler loaded from one jsrc and saved into another registers once."""
    state = {**_state(), "events": cli.events.EventHooks()}
    cli._handle_command("/on turn_start set compact.auto off", state, None)
    cli._handle_command("/on turn_start set compact.auto off", state, None)

    assert len(state["events"].handlers_for("turn_start")) == 1
