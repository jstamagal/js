"""/set in the REPL: what it prints, what NAME selects, and /help set NAME."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from js import cli, compaction, settings
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


def test_set_group_lists_that_group(repl, capsys):
    lines = _out("/set compaction", repl, capsys)
    group = [spec.key for spec in settings.REGISTRY if spec.section == "compaction"]
    assert [line.split()[1] for line in lines] == group


def test_set_wildcard_lists_names_that_match(repl, capsys):
    lines = _out("/set *_FILE", repl, capsys)
    files = [spec.key for spec in settings.REGISTRY if spec.key.endswith("_FILE")]
    assert files and [line.split()[1] for line in lines] == files


def test_set_context_size_is_the_window_of_the_next_request(repl, capsys):
    cfg, state = repl
    _out("/set CONTEXT_SIZE 123456", repl, capsys)
    active = cli._cfg_for_live_state(cfg, state)
    assert compaction.configured_context_window(active, lambda: None) == 123456


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
