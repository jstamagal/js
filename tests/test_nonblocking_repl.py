"""The async REPL drives one real turn end-to-end on its loop: Enter handler →
queue → supervised _do_turn → run_turn_async → persist → clean EOF shutdown.
Headless: a stub app feeds lines, run_turn_async is stubbed, and stdio capture
is neutralized (prompt_toolkit's terminal machinery is its own concern)."""

from __future__ import annotations


import asyncio
from dataclasses import replace

import pytest

from js.mcp.client import MCPClient
from js.mcp.host import MCPHost
from js.mcp_config import MCPConfiguration, MCPPolicy, MCPServer
from test_mcp_client import SpecServer

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
    monkeypatch.delenv("JS_DEFAULT_AGENT", raising=False)
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


@pytest.mark.parametrize("mode", ["now", "batch"])
@pytest.mark.parametrize("busy", [False, True])
def test_mcp_channel_wakes_idle_repl_and_persists_literal_content(monkeypatch, tmp_path, mode, busy):
    monkeypatch.setattr(cli, "_steer_mode", lambda _state: mode)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    server = SpecServer()
    config = MCPConfiguration(
        (MCPServer("channel", "channel", "stdio", command="unused"),), MCPPolicy()
    )
    host = MCPHost(
        config,
        client_factory=lambda _factory, **kwargs: MCPClient(server.factory, **kwargs),
    )
    from repl_driver import repl_state

    def state_with_host(*args, **kwargs):
        state, spec = repl_state(*args, **kwargs)
        state["mcp_host"] = host
        return state, spec

    monkeypatch.setattr("repl_driver.repl_state", state_with_host)
    received = asyncio.Event()
    started = asyncio.Event()
    release = asyncio.Event()
    content = "/exit @/not-a-local-attachment !{sh false}"
    turns = []

    async def turn(cfg, system, messages, telemetry, **kwargs):
        turns.append(messages[-1]["content"])
        if busy and len(turns) == 1:
            started.set()
            await release.wait()
        messages.append({"role": "assistant", "content": "received"})
        if len(turns) == (2 if busy else 1):
            received.set()

    async def incoming(on_line):
        await host.discover(source="channel")
        if busy:
            await on_line("first turn")
            await asyncio.wait_for(started.wait(), timeout=2)
        await server.connections[0].notify(
            "notifications/claude/channel",
            {"content": content, "meta": {"chat_id": "42"}},
        )
        release.set()
        await asyncio.wait_for(received.wait(), timeout=2)

    monkeypatch.setattr(cli.runtime, "run_turn_async", turn)
    run_async(monkeypatch, replace(from_env(), mcp=config), incoming)
    assert len(turns) == (2 if busy else 1)
    assert content in turns[-1]
    assert "42" in turns[-1]
    reloaded = load_messages(_session_file(tmp_path))
    assert [m["role"] for m in reloaded] == ["user", "assistant"] * len(turns)
    assert reloaded[-2]["content"] == turns[-1]
    assert host.channel_sink is None


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
