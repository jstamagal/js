"""A swarm agent's ``tools``: its whole tool surface, which no profile widens
and no turn on its context, its subagents' included, gets past."""

from __future__ import annotations

import asyncio
import dataclasses
import io
import json
from dataclasses import replace

import pytest
import turn_driver

from js import runtime, swarm, swarm_run
from js.toolkit import ToolContext, bus_tools
from js.toolkit.subagents import _child_context

BUS = frozenset(tool.name for tool in bus_tools.tools())


def _agent_dir(tmp_path, monkeypatch, manifest: str | None = None):
    (tmp_path / ".git").mkdir(exist_ok=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JS_DEFAULT_AGENT", "voice")
    monkeypatch.setenv("JS_MODEL", "offline-test-model")
    directory = tmp_path / ".js" / "agents" / "voice"
    directory.mkdir(parents=True)
    (directory / "00-role.md").write_text("SYSTEM", encoding="utf-8")
    if manifest is not None:
        (directory / "agent.yaml").write_text(manifest, encoding="utf-8")


def _surface(tmp_path, **entry) -> set[str]:
    spec = swarm_run.AgentSpec.from_dict({"name": "a", "agent": "voice", "opener": "go", "cwd": str(tmp_path), **entry})
    return set(swarm_run._configure(spec).registry.by_name)


def test_tools_is_a_field_of_the_agent_spec():
    assert "tools" in {f.name for f in dataclasses.fields(swarm_run.AgentSpec)}


def test_a_listed_agent_holds_exactly_its_tools_and_the_bus_tools(tmp_path, monkeypatch):
    _agent_dir(tmp_path, monkeypatch, "tools: [shell:eager, write:eager]\n")

    assert _surface(tmp_path, tools=["read:eager", "fs_search:eager"]) == {"read", "fs_search"} | BUS
    assert _surface(tmp_path, tools=[]) == set(BUS)


def test_an_agent_without_tools_keeps_its_agents_chain(tmp_path, monkeypatch):
    _agent_dir(tmp_path, monkeypatch, "tools: [shell:eager, write:eager]\n")

    assert _surface(tmp_path) == {"shell", "write"} | BUS
    assert _surface(tmp_path, tools=None) == {"shell", "write"} | BUS


def test_a_matching_profile_does_not_change_a_listed_agents_tools(tmp_path, monkeypatch):
    _agent_dir(tmp_path, monkeypatch, "tools: [read:eager]\n"
                                      "models: [{match: offline, tools: [write:eager]}]\n")
    monkeypatch.setenv("JS_TOOL_PROFILES", json.dumps([{"match": "offline", "tools": ["shell:eager"]}]))

    # Unlisted, the agent's `replace` profile decides the surface, its core entry with it.
    unlisted = _surface(tmp_path)
    assert {"write", "skill"} <= unlisted and "read" not in unlisted
    assert _surface(tmp_path, tools=["read:eager"]) == {"read"} | BUS


def test_a_tool_profiles_entry_does_not_change_a_listed_agents_tools(tmp_path, monkeypatch):
    _agent_dir(tmp_path, monkeypatch, "tools: [read:eager]\n")
    monkeypatch.setenv("JS_TOOL_PROFILES", json.dumps([{"match": "offline", "tools": ["shell:eager"]}]))

    assert "shell" in _surface(tmp_path)
    assert _surface(tmp_path, tools=["read:eager"]) == {"read"} | BUS


@pytest.mark.parametrize("entry", ["no_such_tool:eager", "nosuch*:lazy"])
def test_an_entry_that_matches_no_tool_fails_the_agent(tmp_path, monkeypatch, entry):
    _agent_dir(tmp_path, monkeypatch)
    spec = swarm_run.AgentSpec.from_dict({"name": "a", "agent": "voice", "opener": "go", "cwd": str(tmp_path),
                                          "tools": ["read:eager", entry]})

    with pytest.raises(ValueError):
        swarm_run._configure(spec)


def test_a_runner_ends_an_agent_whose_entry_matches_no_tool(tmp_path, monkeypatch):
    _agent_dir(tmp_path, monkeypatch)
    monkeypatch.setattr(runtime.model_client, "stream_model_async",
                        lambda **_: pytest.fail("a failed agent calls no model"))
    path = tmp_path / "spec.json"
    path.write_text(json.dumps({"root": str(tmp_path / "bus"), "agents": [
        {"name": "a", "agent": "voice", "opener": "go", "cwd": str(tmp_path), "tools": ["no_such_tool:eager"]}]}))
    out = io.StringIO()

    assert swarm_run.run(str(path), out=out) == 1
    events = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [e["reason"] for e in events if e["type"] == "agent_end"] == ["error"]


def test_a_listed_agent_has_no_mcp_servers(tmp_path, monkeypatch):
    _agent_dir(tmp_path, monkeypatch)
    servers = object()
    real = swarm_run._agent_config
    monkeypatch.setattr(swarm_run, "_agent_config", lambda spec, **kw: replace(real(spec, **kw), mcp=servers))
    spec = swarm_run.AgentSpec.from_dict({"name": "a", "agent": "voice", "opener": "go", "cwd": str(tmp_path)})

    assert swarm_run._configure(spec).config.mcp is servers
    assert swarm_run._configure(replace(spec, tools=("read:eager",))).config.mcp is None


def test_a_recruit_and_a_restart_keep_the_list(tmp_path, monkeypatch):
    root = tmp_path / "bus"
    added: list[swarm_run.AgentSpec] = []
    monkeypatch.setattr(swarm_run.Runner, "add", lambda self, spec: added.append(spec))
    parent = swarm_run.AgentSpec.from_dict({"name": "a", "agent": "voice", "opener": "go", "tools": ["read:eager"]})
    runner = swarm_run.Runner(root, [parent], None, io.StringIO())

    async def recruit() -> None:
        await asyncio.to_thread(runner.recruiter(parent, swarm.Agent(root / "a")), "twig", "hello")
        await asyncio.sleep(0)

    asyncio.run(recruit())
    runner.ended["a"] = parent
    runner.act("a", swarm.Msg(seq=1, ts=0.0, sender="steer", to="a", kind=swarm.RESTART, body=""))

    assert [spec.tools for spec in added] == [("read:eager",), ("read:eager",)]


def test_a_turn_on_a_context_with_a_ceiling_offers_only_the_tools_within_it(tmp_path, monkeypatch):
    read, _ = turn_driver.recording("read")
    shell, _ = turn_driver.recording("shell")
    context = ToolContext(cwd=tmp_path)
    context.tool_ceiling = frozenset({"read"})

    turn = turn_driver.run(tmp_path, monkeypatch, turn_driver.registry(read, shell), [], context=context)

    assert turn.offered == [["read"]]


def test_a_turn_on_a_context_with_a_ceiling_starts_no_mcp_server(tmp_path, monkeypatch):
    read, _ = turn_driver.recording("read")
    monkeypatch.setattr(runtime.mcp_hosts, "MCPHost", lambda *a, **k: pytest.fail("no MCP host under a ceiling"))
    servers = type("Servers", (), {"servers": ("files",)})()
    context = ToolContext(cwd=tmp_path)
    context.tool_ceiling = frozenset({"read"})

    turn_driver.run(tmp_path, monkeypatch, turn_driver.registry(read), [],
                    cfg=turn_driver.config(tmp_path, mcp=servers), context=context)


def test_a_subagent_context_inherits_the_ceiling(tmp_path):
    parent = ToolContext(cwd=tmp_path)
    parent.tool_ceiling = frozenset({"read"})

    assert _child_context(parent, turn_driver.registry(), "child").tool_ceiling == frozenset({"read"})
    assert _child_context(ToolContext(cwd=tmp_path), turn_driver.registry(), "child").tool_ceiling is None


def _subagent_offers(tmp_path, monkeypatch, **entry) -> dict[str, set[str]]:
    """The tools a swarm agent's model calls and its `task` child's were offered."""
    _agent_dir(tmp_path, monkeypatch, "tools: [shell:eager, write:eager, task:eager]\n")
    room = swarm.Room(tmp_path / "bus")
    offered: dict[str, set[str]] = {"parent": set(), "child": set()}

    def stream(**kwargs):
        texts = ["".join(p.text for p in m.parts if p.kind == "text") for m in kwargs["messages"] if m.role == "user"]
        names = {tool.name for tool in kwargs.get("tools") or ()}
        if "child-task" in texts[0]:
            offered["child"] |= names
            kwargs["on_text"]("child done")
            return turn_driver.reply(text="child done")
        offered["parent"] |= names
        if len([m for m in kwargs["messages"] if m.role == "assistant"]) == 0:
            return turn_driver.reply(("c1", "task", json.dumps({"tasks": ["child-task"], "agent_id": "voice"})))
        room.send("steer", "*", "enough", kind=swarm.STOP)
        kwargs["on_text"]("ok")
        return turn_driver.reply(text="ok")

    monkeypatch.setattr(runtime.model_client, "stream_model_async", stream)
    path = tmp_path / "spec.json"
    path.write_text(json.dumps({"root": str(tmp_path / "bus"), "agents": [
        {"name": "a", "agent": "voice", "opener": "go", "session": "s-a", "cwd": str(tmp_path), **entry}]}))
    assert swarm_run.run(str(path), out=io.StringIO()) == 0
    return offered


def test_a_listed_agents_subagent_runs_within_its_tools(tmp_path, monkeypatch):
    offered = _subagent_offers(tmp_path, monkeypatch, tools=["read:eager", "task:eager"])

    assert offered["parent"] == {"read", "task"} | BUS
    assert offered["child"] and offered["child"] <= {"read", "task"} | BUS


def test_an_unlisted_agents_subagent_gets_its_own_chain(tmp_path, monkeypatch):
    offered = _subagent_offers(tmp_path, monkeypatch)

    assert {"shell", "write"} <= offered["child"]
