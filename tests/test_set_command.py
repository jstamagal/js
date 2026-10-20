"""/set in the REPL: what it prints, what NAME selects, and /help set NAME."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from js import cli, compaction, keys, settings
from js import messages as msgs
from js.config import from_env, jsrc_paths
from repl_driver import repl_state


@pytest.fixture
def repl(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(msgs, "BANNER", msgs.BANNER)
    os.chdir(tmp_path)
    cfg = from_env()
    state, _spec = repl_state(cfg)
    cli._run_rc_commands(state, cfg, jsrc_paths(Path(cfg.project_dir or tmp_path)))
    return cfg, state


def _out(line, repl, capsys) -> list[str]:
    cfg, state = repl
    capsys.readouterr()
    assert cli._handle_command(line, state, cfg) is True
    return capsys.readouterr().out.splitlines()


def test_set_name_is_one_line_led_by_the_banner_setting(repl, capsys):
    _out("/set BANNER -!-", repl, capsys)
    lines = _out("/set model", repl, capsys)
    assert len(lines) == 1
    assert lines[0].startswith("-!- ")
    assert "MODEL" in lines[0]


def test_set_group_lists_that_group(repl, capsys):
    lines = _out("/set compaction", repl, capsys)
    group = [spec.key for spec in settings.REGISTRY if spec.section == "compaction"]
    assert [line.split()[1] for line in lines] == group


def test_set_wildcard_lists_names_that_match(repl, capsys):
    lines = _out("/set *_FILE", repl, capsys)
    files = [spec.key for spec in settings.REGISTRY if spec.key.endswith("_FILE")]
    assert files and [line.split()[1] for line in lines] == files


def test_every_setting_has_help():
    assert [spec.key for spec in settings.REGISTRY if not spec.doc] == []


def test_help_set_name_prints_that_settings_help(repl, capsys):
    lines = _out("/help set effort", repl, capsys)
    assert lines[0] == "EFFORT"
    assert " ".join(line.strip() for line in lines[1:]) == settings.spec_for("EFFORT").doc


def test_a_typed_command_line_is_not_echoed_and_a_message_is(repl):
    _cfg, state = repl
    assert cli._echoes("/set model", state) is False
    assert cli._echoes("hello there", state) is True
    assert cli._echoes("/usr/bin/python is broken", state) is True
    assert cli._echoes("/README.md", state) is True


def test_an_invalid_command_is_not_echoed(repl):
    _cfg, state = repl
    assert cli._echoes("/sett EFFORT high", state) is False


def test_setting_banner_answers_under_the_new_banner(repl, capsys):
    assert _out("/set BANNER -!-", repl, capsys)[0].startswith("-!- ")


def test_set_context_size_is_the_window_of_the_next_request(repl, capsys):
    cfg, state = repl
    _out("/set CONTEXT_SIZE 123456", repl, capsys)
    active = cli._cfg_for_live_state(cfg, state)
    assert compaction.configured_context_window(active, lambda: None) == 123456


def test_no_group_is_named_like_a_setting():
    groups = {spec.section for spec in settings.REGISTRY}
    assert [group for group in groups if settings.spec_for(group) is not None] == []


def test_the_blocking_repl_echoes_a_message_and_not_a_command(repl):
    _cfg, state = repl
    assert cli._blocking_echo("/set model", state) is None
    assert "hello" in cli._blocking_echo("hello", state)


def test_the_blocking_prompt_erases_its_own_copy_of_a_sent_line(repl, tmp_path):
    cfg, _state = repl
    keymap, _errors = keys.load(tmp_path / "no-keys")
    session = cli._repl_prompt_session(cfg, keymap=keymap, completer=None)
    assert session.app.erase_when_done is True


def test_set_model_shows_the_model_the_agent_runs(repl, capsys):
    from dataclasses import replace

    cfg, state = repl
    spec = replace(state["prompt_spec"], model="deepseek/review-agent-pick")
    agent_cfg = replace(cfg, model="review-agent-pick", provider_id="deepseek")
    agent_state = cli._repl_state(agent_cfg, spec, messages=[], live_settings=state["settings"])
    capsys.readouterr()
    assert cli._handle_command("/set model", agent_state, agent_cfg) is True
    [line] = capsys.readouterr().out.splitlines()
    assert "review-agent-pick" in line and f"agent {cfg.agent_id}" in line
