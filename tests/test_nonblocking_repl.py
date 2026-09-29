"""The async REPL drives one real turn end-to-end on its loop: Enter handler →
queue → supervised _do_turn → run_turn_async → persist → clean EOF shutdown.
Headless: a stub app feeds lines, run_turn_async is stubbed, and stdio capture
is neutralized (prompt_toolkit's terminal machinery is its own concern)."""

from __future__ import annotations

import pytest

from js import cli
from js.config import from_env
from js.memory import load_messages
from repl_driver import run_async


def _session_file(tmp_path):
    found = list((tmp_path / ".js" / "sessions").rglob("*.jsonl"))
    assert len(found) == 1, found
    return found[0]


def _drive_async_repl(monkeypatch, tmp_path, lines, run_turn_async_stub):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_AGENT", raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.runtime, "run_turn_async", run_turn_async_stub)
    return run_async(monkeypatch, from_env(), lines)


def test_nonblocking_repl_runs_a_turn_and_persists(monkeypatch, tmp_path):
    async def run_turn_async_stub(cfg, system, messages, telemetry, **kwargs):
        messages.append({"role": "assistant", "content": "did the thing"})

    _drive_async_repl(monkeypatch, tmp_path, ["please do the thing"], run_turn_async_stub)

    reloaded = load_messages(_session_file(tmp_path))
    assert [m["role"] for m in reloaded] == ["user", "assistant"]
    assert reloaded[0]["content"] == "please do the thing"
    assert reloaded[1]["content"] == "did the thing"


def test_nonblocking_repl_empty_line_then_eof_is_clean(monkeypatch, tmp_path):
    async def run_turn_async_stub(cfg, system, messages, telemetry, **kwargs):
        raise AssertionError("no turn should run for a blank line")

    _drive_async_repl(monkeypatch, tmp_path, ["   "], run_turn_async_stub)
    assert load_messages(_session_file(tmp_path)) == []


def test_nonblocking_repl_skill_line_sends_user_only_skill(monkeypatch, tmp_path):
    skill = tmp_path / ".js" / "skills" / "secret" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\ndisable-model-invocation: true\n---\nuser-only body\n", encoding="utf-8")
    turns: list[str] = []

    async def run_turn_async_stub(cfg, system, messages, telemetry, **kwargs):
        turns.append(str(messages[-1]["content"]))
        messages.append({"role": "assistant", "content": "ok"})

    lines = ["/skill nosuch", "/skill secret check the plan"]
    _drive_async_repl(monkeypatch, tmp_path, lines, run_turn_async_stub)

    # The unknown name starts no turn; the user-only skill reaches the model.
    assert len(turns) == 1
    assert "user-only body" in turns[0]
    assert "check the plan" in turns[0]
    reloaded = load_messages(_session_file(tmp_path))
    assert [m["role"] for m in reloaded] == ["user", "assistant"]
    assert "user-only body" in str(reloaded[0]["content"])


def test_tui_flag_is_an_unknown_argument(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        cli.main(["--tui"])
    assert exc.value.code == 2


def test_turn_state_commands_are_refused_while_a_turn_runs():
    """Commands that clear/rotate/compact the live message list must be gated while a
    turn is active — running them off-loop races the turn's single-writer append."""
    assert cli._is_turn_state_command("/reset")
    assert cli._is_turn_state_command("/wipe")
    assert cli._is_turn_state_command("/compact")
    assert cli._is_turn_state_command("/compact up to here")
    # non-mutating / unrelated commands stay live
    assert not cli._is_turn_state_command("/compact-auto on")
    assert not cli._is_turn_state_command("/turns")
    assert not cli._is_turn_state_command("/model gpt")
    assert not cli._is_turn_state_command("hello there")
