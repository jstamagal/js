"""The swarm bus (js.swarm): inboxes, delivery at tool boundaries, sleep and wake.

  js -p --json --swarm ROOT/NAME "opener"
"""

from __future__ import annotations

import io
import json
import threading
import time

import ai
import ai.types.usage
from ai.providers import history_utils

from js import cli, headless, runtime, swarm
from js.config import Config
from js.model_client import ModelStreamResult, ModelToolCall
from js.toolkit import ToolContext
from js.toolkit.core import Tool
from js.toolkit.registry import ToolRegistry


# --------------------------------------------------------------------------
# the bus
# --------------------------------------------------------------------------

def test_send_lands_in_each_target_inbox_and_the_log(tmp_path):
    room = swarm.Room(tmp_path / "bus")
    for name in ("a", "b", "c"):
        room.join(name)
    one = room.send("a", "b", "psst")
    everyone = room.send("a", "*", "hello all")
    assert (one.seq, everyone.seq) == (1, 2)
    assert [m.seq for m in room.join("b").drain()] == [1, 2]
    assert [m.seq for m in room.join("c").drain()] == [2]
    assert room.join("a").drain() == []  # a broadcast skips its sender
    assert [json.loads(line)["seq"] for line in room.log.read_text().splitlines()] == [1, 2]


def test_a_message_waits_for_an_agent_that_has_not_joined_yet(tmp_path):
    room = swarm.Room(tmp_path / "bus")
    room.send("runner", "late", "opener")
    assert [m.body for m in room.join("late").drain()] == ["opener"]


def test_drain_hands_each_message_over_once_in_seq_order(tmp_path):
    room = swarm.Room(tmp_path / "bus")
    inbox = room.join("a")
    for i in range(3):
        room.send("b", "a", f"m{i}")
    assert [m.body for m in inbox.drain()] == ["m0", "m1", "m2"]
    assert inbox.drain() == []


def test_wait_returns_when_a_message_lands_and_empty_on_timeout(tmp_path):
    room = swarm.Room(tmp_path / "bus")
    inbox = room.join("a")
    assert inbox.wait(timeout=0.3) == []
    threading.Timer(0.2, lambda: room.send("b", "a", "ping")).start()
    started = time.monotonic()
    assert [m.body for m in inbox.wait(timeout=5)] == ["ping"]
    assert time.monotonic() - started < 2


# --------------------------------------------------------------------------
# delivery at the tool boundary (the steer hook, see tests/test_steering.py)
# --------------------------------------------------------------------------

def _cfg(tmp_path):
    base = tmp_path / "s" / "a"
    return Config(
        agent_id="a", agent_dir=base, model="offline-test-model", provider_id=None,
        provider_base_url=None, provider_api_key=None, reasoning_effort=None, max_output_tokens=None,
        max_tool_iterations=4, max_bash_output_bytes=65536, max_tool_result_bytes=65536,
        fetch_timeout_s=5, debug_log=None, trace=False, history_file=base / ".history",
        sessions_dir=base, session_file=base / "auto.jsonl", prompts_dir=tmp_path / "p" / "a",
    )


def _text(text: str) -> ModelStreamResult:
    return ModelStreamResult(text=text, tool_calls=[], reasoning="",
                             usage=ai.types.usage.Usage(input_tokens=1, output_tokens=1),
                             finish_reason="stop", assistant_message=ai.assistant_message(text))


def _call(name: str, call_id: str) -> ModelStreamResult:
    message = ai.types.messages.Message(
        role="assistant",
        parts=[ai.types.messages.ToolCallPart(tool_call_id=call_id, tool_name=name, tool_args="{}")],
    )
    return ModelStreamResult(text="", tool_calls=[ModelToolCall(id=call_id, name=name, arguments="{}")],
                             reasoning="", usage=ai.types.usage.Usage(input_tokens=1, output_tokens=1),
                             finish_reason="tool_calls", assistant_message=message)


def test_inbox_messages_reach_the_model_at_the_tool_boundary(monkeypatch, tmp_path):
    agent = swarm.Agent(tmp_path / "bus" / "a")
    seen: list[list] = []

    async def model(**kwargs):
        convo = kwargs["messages"]
        history_utils.validate(convo)
        seen.append(list(convo))
        return _call("probe", "c1") if len(seen) == 1 else _text("done")

    def probe() -> str:
        agent.room.send("b", "a", "look left")  # lands while the tool runs
        return "probed"

    monkeypatch.setattr(runtime.model_client, "stream_model_async", model)
    registry = ToolRegistry(tools=(Tool("probe", "Probe.", probe, {}),), aliases={})
    messages = [{"role": "user", "content": "go"}]

    runtime.run_turn(_cfg(tmp_path), "SYS", messages, runtime.Telemetry(debug_log=None),
                     tool_registry=registry, tool_context=ToolContext(cwd=tmp_path), suppress_output=True,
                     steer=agent.steer)

    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "user", "assistant"]
    assert messages[3].get("steered") is True
    assert "[#1 b -> you" in messages[3]["content"] and "look left" in messages[3]["content"]


# --------------------------------------------------------------------------
# the tools --swarm puts on the surface
# --------------------------------------------------------------------------

def test_send_tool_posts_from_the_agent_on_the_context(tmp_path):
    agent = swarm.Agent(tmp_path / "bus" / "a")
    agent.room.join("b")
    registry = swarm.with_bus_tools(ToolRegistry(tools=(), aliases={}))
    send, who = registry.resolve("send"), registry.resolve("who")
    on_bus = ToolContext(cwd=tmp_path, swarm=agent)

    assert send.handler(to="b", text="hi b", context=on_bus) == "sent #1 to b"
    assert [m.body for m in agent.room.join("b").drain()] == ["hi b"]
    assert who.handler(context=on_bus) == "a (you)\nb"
    assert send.handler(to="b", text="x", context=ToolContext(cwd=tmp_path)).startswith("ERROR")


# --------------------------------------------------------------------------
# headless: a turn ends, the agent sleeps, a message wakes it
# --------------------------------------------------------------------------

def _agent_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JS_AGENT", "voice")
    monkeypatch.setenv("JS_MODEL", "offline-test-model")
    monkeypatch.setattr(runtime.T, "STOCK_CONTEXT", ToolContext(cwd=tmp_path))
    directory = tmp_path / ".js" / "agents" / "voice"
    directory.mkdir(parents=True)
    (directory / "00-role.md").write_text("SYSTEM", encoding="utf-8")


def test_a_swarm_agent_sleeps_after_its_turn_and_wakes_on_a_message(tmp_path, monkeypatch):
    _agent_dir(tmp_path, monkeypatch)
    room = swarm.Room(tmp_path / "bus")
    turns: list[list[str]] = []

    def stream(**kwargs):
        turns.append(["".join(p.text for p in m.parts if p.kind == "text")
                      for m in kwargs["messages"] if m.role == "user"])
        if len(turns) == 1:
            threading.Timer(0.3, lambda: room.send("b", "a", "ping")).start()
            answer = "first"
        else:
            room.send("steer", "a", "enough", kind="stop")  # lands during the turn; honored after it
            answer = "second"
        kwargs["on_text"](answer)
        return ModelStreamResult(text=answer, tool_calls=[], reasoning="",
                                 usage=ai.types.usage.Usage(input_tokens=0, output_tokens=1),
                                 finish_reason="stop", assistant_message=ai.assistant_message(answer))

    monkeypatch.setattr(runtime.model_client, "stream_model_async", stream)
    out = io.StringIO()

    code = cli._run_prompt("go", session="s1", swarm=str(tmp_path / "bus" / "a"), events=headless.JsonEvents(out))

    assert code == 0
    assert len(turns) == 2
    assert "ping" in turns[1][-1] and "[#1 b -> you" in turns[1][-1]
    kinds = [json.loads(line)["type"] for line in out.getvalue().splitlines()]
    assert kinds.count("turn_start") == 2
    second_turn = kinds.index("turn_start", kinds.index("turn_start") + 1)
    assert kinds.index("sleep") < kinds.index("wake") < second_turn
    assert kinds[-1] == "wake"  # the stop was the last thing it woke to; no turn ran on it
