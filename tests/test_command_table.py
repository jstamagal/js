"""The one command table: /save writes everything resident, a restart replays
it, and a jsrc may hold any command line."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

from repl_driver import output_of, record_turns, repl_state, run_async, type_into_js

from js import cli, paths, settings
from js.config import from_env, jsrc_paths
from js.memory import append_message


def _repl_cfg(tmp_path, monkeypatch):
    """The Config js starts with in `tmp_path`, on a session file of its own."""
    monkeypatch.chdir(tmp_path)
    return replace(from_env(), session_file=tmp_path / "session.jsonl")


def test_assembly_command_is_dispatched_and_completed(tmp_path, monkeypatch):
    def clear(arg, state, cfg):
        state.messages.clear()

    cfg = _repl_cfg(tmp_path, monkeypatch)
    append_message(cfg.session_file, {"role": "user", "content": "old"})
    turns = record_turns(monkeypatch)
    assembly = cli.ReplAssembly(commands={"clear": cli.Command(clear, "clear", cli.msgs.CMD_RESET)})

    state = run_async(monkeypatch, cfg, ["/clear", "new"], wait=True, assembly=assembly)

    [turn] = turns
    assert [message["content"] for message in turn.messages] == ["new"]
    assert "clear" in cli._command_completions(state)
    assert "reset" not in cli._command_completions(state)


def _launch(cwd):
    """A REPL launch in `cwd`: the config, the state after replaying the jsrc
    files, and the replay's errors."""
    os.chdir(cwd)
    cfg = from_env()
    state, _spec = repl_state(cfg)
    errors = cli._run_rc_commands(state, cfg, jsrc_paths(Path(cfg.project_dir or cwd)))
    return cfg, state, errors


def _run(monkeypatch, lines):
    """Type each of `lines` into js started here; none of them is a turn."""
    turns = record_turns(monkeypatch)
    assert type_into_js(monkeypatch, lines) == 0
    assert turns == []


def test_save_then_restart_brings_back_handlers_and_aliases(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _run(monkeypatch, [
        "/on turn_start set COMPACTION_AUTO off",
        "/on ^tool_call set TRANSCRIPT_LOG on",
        "/alias ca compact-auto $*",
        "/save",
    ])

    _cfg, state, errors = _launch(tmp_path)

    assert errors == []
    assert [h.handler for h in state.events.handlers_for("turn_start")] == ["set COMPACTION_AUTO off"]
    assert [(h.handler, h.suppress) for h in state.events.handlers_for("tool_call")] == [
        ("set TRANSCRIPT_LOG on", True)
    ]
    assert state.aliases == {"ca": "compact-auto $*"}


def test_save_keeps_the_jsrc_and_writes_only_what_changed(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    jsrc = paths.global_config_file()
    jsrc.parent.mkdir(parents=True, exist_ok=True)
    jsrc.write_text("# old\nset READ_LINES_MAX 33\n", encoding="utf-8")
    _run(monkeypatch, ["/set COMPACTION_AUTO off", "/save"])

    assert jsrc.read_text(encoding="utf-8") == "# old\nset READ_LINES_MAX 33\nset COMPACTION_AUTO off\n"


def test_jsrc_model_and_provider_lines_take_effect(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    jsrc = paths.global_config_file()
    jsrc.parent.mkdir(parents=True, exist_ok=True)
    jsrc.write_text("/provider deepseek\n/model deepseek-chat\n", encoding="utf-8")

    cfg, state, _errors = _launch(tmp_path)
    turn_cfg = state.config_for_turn(cfg)

    assert (turn_cfg.model, turn_cfg.provider_id) == ("deepseek-chat", "deepseek")


def test_rc_replay_leaves_set_lines_to_the_settings_layer(monkeypatch, tmp_path):
    """Env beats jsrc for settings; replaying the rc at startup must not undo that."""
    monkeypatch.chdir(tmp_path)
    jsrc = paths.global_config_file()
    jsrc.parent.mkdir(parents=True, exist_ok=True)
    jsrc.write_text("set COMPACTION_AUTO off\nalias t turns\n", encoding="utf-8")
    monkeypatch.setenv("JS_COMPACTION_AUTO", "on")

    _cfg, state, _errors = _launch(tmp_path)

    assert settings.get_dotted(state.settings, ("COMPACTION_AUTO",)) is True
    assert state.aliases == {"t": "turns"}


def test_jsrc_load_resolves_beside_the_jsrc_and_stays_under_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    jsrc = paths.global_config_file()
    jsrc.parent.mkdir(parents=True, exist_ok=True)
    jsrc.write_text("load extra.irc\n", encoding="utf-8")
    (jsrc.parent / "extra.irc").write_text("set COMPACTION_AUTO off\nalias t turns\n", encoding="utf-8")
    monkeypatch.setenv("JS_COMPACTION_AUTO", "on")
    project = tmp_path / "project"
    project.mkdir()

    _cfg, state, errors = _launch(project)

    assert errors == []
    assert state.aliases == {"t": "turns"}
    assert settings.get_dotted(state.settings, ("COMPACTION_AUTO",)) is True


def test_settings_in_a_jsrc_loaded_file_apply_at_config_load(tmp_path):
    jsrc = tmp_path / "jsrc"
    jsrc.write_text("load sub/extra.irc\nload jsrc\n", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "extra.irc").write_text("load ../more.irc\n", encoding="utf-8")
    (tmp_path / "more.irc").write_text("set COMPACTION_AUTO off\n", encoding="utf-8")

    live = settings.collect_settings(config_paths=[jsrc], env={})

    assert settings.get_dotted(live, ("COMPACTION_AUTO",)) is False


def test_rc_errors_name_the_line_and_do_not_stop_startup(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    jsrc = paths.global_config_file()
    jsrc.parent.mkdir(parents=True, exist_ok=True)
    jsrc.write_text("bogus verb\nalias t turns\n", encoding="utf-8")

    _cfg, state, errors = _launch(tmp_path)

    assert len(errors) == 1
    assert f"{jsrc}:1" in errors[0]
    assert state.aliases == {"t": "turns"}


def test_alias_runs_its_body_with_arguments(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    turns = record_turns(monkeypatch)

    assert type_into_js(monkeypatch, [
        "/alias ca compact-auto $*",
        "/alias trace set TRANSCRIPT_LOG",
        "/ca off",
        "/trace off",
        "hello",
    ]) == 0

    [turn] = turns
    assert turn.messages[-1]["content"] == "hello"
    assert settings.get_dotted(turn.settings, ("COMPACTION_AUTO",)) is False
    assert settings.get_dotted(turn.settings, ("TRANSCRIPT_LOG",)) is False


def test_alias_cannot_shadow_a_command_and_can_be_removed(tmp_path, monkeypatch):
    """/save writes every alias the session defined, so a save that writes
    none means there is none."""
    monkeypatch.chdir(tmp_path)

    _run(monkeypatch, ["/alias set turns", "/alias t turns", "/alias -t", "/save"])

    jsrc = paths.global_config_file()
    written = jsrc.read_text(encoding="utf-8").splitlines() if jsrc.exists() else []
    assert [line for line in written if line.startswith("alias")] == []


def test_self_referencing_alias_stops(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    turns = record_turns(monkeypatch)
    printed: list[str] = []

    async def typing(enter):
        await enter("/alias loop loop")
        printed.append(await output_of(enter, capsys, "/loop"))

    assert type_into_js(monkeypatch, typing) == 0

    assert turns == []
    assert printed[0]


def test_unknown_slash_word_is_not_a_command(tmp_path, monkeypatch):
    """A line like `/usr/bin/python is broken` is prose for the model."""
    monkeypatch.chdir(tmp_path)
    turns = record_turns(monkeypatch)

    assert type_into_js(monkeypatch, ["/usr/bin/python is broken"]) == 0

    [turn] = turns
    assert turn.messages[-1]["content"] == "/usr/bin/python is broken"


def test_skill_with_a_name_is_a_turn_and_bare_skill_is_a_command(tmp_path, monkeypatch):
    """`/skill <name> [request]` becomes a user message; bare `/skill` lists."""
    monkeypatch.chdir(tmp_path)
    turns = record_turns(monkeypatch)

    assert type_into_js(monkeypatch, ["/skill grilling check the plan"]) == 0

    assert len(turns) == 1
    assert "skill" in cli.COMMANDS
    assert "tools" in cli.COMMANDS


def test_turn_state_commands_come_from_the_table(monkeypatch):
    command = cli.Command(lambda arg, state, cfg: None, "scrub", "test entry", turn_state=True)
    monkeypatch.setitem(cli.COMMANDS, "scrub", command)

    assert cli._is_turn_state_command("/scrub now")
    assert not cli._is_turn_state_command("/turns")


def test_registering_the_same_handler_twice_keeps_one(tmp_path, monkeypatch):
    """A handler loaded from one jsrc and saved into another registers once."""
    cfg = _repl_cfg(tmp_path, monkeypatch)

    state = run_async(monkeypatch, cfg, ["/on turn_start set COMPACTION_AUTO off"] * 2)

    assert len(state.events.handlers_for("turn_start")) == 1


def test_disabled_skills_survive_save_and_restart(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _run(monkeypatch, ['/set SKILLS_DISABLED ["grilling", "handoff"]', "/save"])

    cfg, state, errors = _launch(tmp_path)

    assert errors == []
    assert state.config_for_turn(cfg).skills_disabled == ("grilling", "handoff")
