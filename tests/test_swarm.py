"""The swarm bus (js.swarm): inboxes, delivery at tool boundaries, sleep and wake.

  js -p --json --swarm ROOT/NAME "opener"
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
import threading
import time
from pathlib import Path

import ai
import ai.types.usage
import pytest
from ai.providers import history_utils

from js import cli, headless, memory, runtime, swarm, swarm_run
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
    roster = who.handler(context=on_bus).splitlines()
    assert [line.split()[0] for line in roster] == ["a", "b"] and "(you)" in roster[0]
    assert send.handler(to="b", text="x", context=ToolContext(cwd=tmp_path)).startswith("ERROR")


def test_a_subscriber_gets_every_message_of_that_kind(tmp_path):
    room = swarm.Room(tmp_path / "bus")
    for name in ("a", "b", "c"):
        room.join(name)
    room.subscribe("c", "claim")
    room.send("a", "b", "taking the parser", kind="claim")
    room.send("a", "b", "psst")
    room.send("c", "b", "the lexer is mine", kind="claim")  # not echoed to its sender
    assert [m.body for m in room.join("c").drain()] == ["taking the parser"]
    assert [m.body for m in room.join("b").drain()] == ["taking the parser", "psst", "the lexer is mine"]
    room.subscribe("c", "claim", on=False)
    room.send("a", "b", "and the printer", kind="claim")
    assert room.join("c").drain() == []


def test_a_claim_has_one_holder_until_released_or_expired(tmp_path, monkeypatch):
    room = swarm.Room(tmp_path / "bus")
    assert room.claim("parser", "a", ttl=60)[:2] == (True, "a")
    assert room.claim("parser", "b", ttl=60)[:2] == (False, "a")
    assert room.claim("parser", "a", ttl=60)[0] is True  # the holder renews
    assert room.release("parser", "b") is False
    assert room.release("parser", "a") is True
    assert room.claim("parser", "b", ttl=1)[0] is True
    now = time.time()
    monkeypatch.setattr(swarm.time, "time", lambda: now + 5)
    assert room.claim("parser", "a", ttl=60)[:2] == (True, "a")  # b's claim expired
    assert set(room.claims()) == {"parser"}


def test_a_burst_is_one_wake(tmp_path):
    agent = swarm.Agent(tmp_path / "bus" / "a")

    def burst():
        agent.room.send("b", "a", "one")
        time.sleep(0.2)
        agent.room.send("b", "a", "two")

    threading.Timer(0.1, burst).start()
    assert [m.body for m in agent.sleep()] == ["one", "two"]


def test_wake_me_wakes_the_agent_with_a_tick_when_nothing_lands(tmp_path):
    agent = swarm.Agent(tmp_path / "bus" / "a")
    agent.wake_me(0.3)
    started = time.monotonic()
    landed = agent.sleep()
    assert [(m.sender, m.kind) for m in landed] == [(swarm.CLOCK, swarm.TICK)]
    assert 0.2 < time.monotonic() - started < 3
    assert agent.alarm is None


def test_quiet_means_every_member_asleep_with_an_empty_inbox(tmp_path):
    room = swarm.Room(tmp_path / "bus")
    assert room.quiet() is False  # nobody on the bus
    a, b = swarm.Agent(tmp_path / "bus" / "a"), swarm.Agent(tmp_path / "bus" / "b")
    assert room.quiet() is False  # both working
    sleepers = [threading.Thread(target=x.sleep, daemon=True) for x in (a, b)]
    for t in sleepers:
        t.start()
    deadline = time.monotonic() + 3
    while not (room.asleep("a") and room.asleep("b")) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert room.quiet() is True
    room.send("steer", "a", "go")
    assert room.quiet() is False  # a has mail, then a is awake
    sleepers[0].join(3)
    assert room.asleep("a") is False and room.quiet() is False
    room.send("steer", "b", "go")
    sleepers[1].join(3)


def test_retire_posts_the_handoff_and_leaves_the_bus(tmp_path):
    a = swarm.Agent(tmp_path / "bus" / "a")
    a.room.join("b")
    retire = swarm.with_bus_tools(ToolRegistry(tools=(), aliases={})).resolve("retire")
    out = retire.handler(handoff="parser done; lexer left; branch feat/x", context=ToolContext(cwd=tmp_path, swarm=a))
    assert not out.startswith("ERROR")
    assert a.stop_seen is True
    assert a.room.members() == ["b"]
    assert [(m.kind, m.body) for m in a.room.join("b").drain()] == [(swarm.RETIRE, "parser done; lexer left; branch feat/x")]


def test_a_posted_cell_tells_everyone_and_sits_open_on_the_board(tmp_path):
    room = swarm.Room(tmp_path / "bus")
    for name in ("a", "b"):
        room.join(name)
    cell = room.post_cell("a", "write the parser", "in src/parse.py; done when tests pass")
    assert (cell.id, cell.status) == (1, "open")
    assert [(m.kind, m.sender) for m in room.join("b").drain()] == [(swarm.TASK, "a")]
    assert [(c.id, c.status, c.holder) for c in room.cells()] == [(1, "open", "")]


def test_a_taken_cell_is_open_again_when_its_hold_expires(tmp_path, monkeypatch):
    room = swarm.Room(tmp_path / "bus")
    room.join("a")
    cell = room.post_cell("a", "write the parser")
    assert room.take_cell(cell.id, "b", ttl=1)[:2] == (True, "b")
    assert room.take_cell(cell.id, "c", ttl=1)[:2] == (False, "b")
    assert [(c.status, c.holder) for c in room.cells()] == [("taken", "b")]
    now = time.time()
    monkeypatch.setattr(swarm.time, "time", lambda: now + 5)
    assert [(c.status, c.holder) for c in room.cells()] == [("open", "")]
    assert room.take_cell(cell.id, "c", ttl=60)[:2] == (True, "c")


def test_finishing_a_cell_frees_it_tells_everyone_and_sinks_it_to_the_bottom(tmp_path):
    room = swarm.Room(tmp_path / "bus")
    for name in ("a", "b", "c"):
        room.join(name)
    first = room.post_cell("a", "write the parser")
    room.post_cell("a", "write the lexer")
    room.take_cell(first.id, "b")
    room.join("c").drain()
    done = room.finish_cell(first.id, "b", "branch feat/parser @ abc123")
    assert (done.status, done.holder, done.result) == ("done", "b", "branch feat/parser @ abc123")
    assert [(m.kind, m.sender) for m in room.join("c").drain()] == [(swarm.DONE, "b")]
    assert first.key not in room.claims()
    assert [(c.id, c.status) for c in room.cells()] == [(2, "open"), (1, "done")]


def test_task_tools_work_the_board_from_the_agent_on_the_context(tmp_path):
    a = swarm.Agent(tmp_path / "bus" / "a")
    registry = swarm.with_bus_tools(ToolRegistry(tools=(), aliases={}))
    post, take, finish, tasks = (registry.resolve(n) for n in ("post_task", "take_task", "finish_task", "tasks"))
    on_bus = ToolContext(cwd=tmp_path, swarm=a)
    assert post.handler(title="write the parser", body="in src", context=on_bus).startswith("posted task #1")
    assert "write the parser" in take.handler(id=1, context=on_bus)
    assert take.handler(id=7, context=on_bus).startswith("no task #7")
    assert finish.handler(id=1, result="done on feat/parser", context=on_bus).startswith("task #1 done")
    assert "done by a" in tasks.handler(context=on_bus)


def test_spawn_runs_the_parents_command_under_a_new_name(tmp_path, monkeypatch):
    a = swarm.Agent(tmp_path / "bus" / "a")

    class FakeProc:
        def __init__(self, cmd, **_kw):
            self.cmd, self.pid, self.stdin = cmd, 4242, io.BytesIO()
            self.stdin.close = lambda: None  # keep the opener readable

    monkeypatch.setattr(swarm.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(swarm.sys, "argv", ["/x/bin/js", "-a", "troop", "-q", "--json", "-s", "troop-1-a",
                                            "--swarm", str(tmp_path / "bus" / "a"), "--extra", "k=v", "-p", "-"])
    proc = a.spawn("twig", "You are twig.")
    assert proc.cmd == [sys.executable, "-m", "js", "-a", "troop", "-q", "--json", "-s", "troop-1-a-twig",
                        "--swarm", str(tmp_path / "bus" / "twig"), "--extra", "k=v", "-p", "-"]
    assert proc.stdin.getvalue() == b"You are twig."
    assert json.loads((tmp_path / "bus" / "twig" / "spawned").read_text())["by"] == "a"
    assert swarm.sibling_argv(["js", "-p", "hello"], "/r/twig", "twig") == [sys.executable, "-m", "js", "-p", "-",
                                                                            "--swarm", "/r/twig"]
    with pytest.raises(ValueError):
        a.spawn("twig", "again")  # already on the bus
    monkeypatch.setattr(swarm, "SPAWN_CAP", 1)
    with pytest.raises(ValueError):
        a.spawn("moss", "one over the cap")


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


# --------------------------------------------------------------------------
# every agent in one process: python -m js.swarm run SPEC.json
# --------------------------------------------------------------------------

def _spec(tmp_path, agents: list[dict], slots=None) -> str:
    spec = {"root": str(tmp_path / "bus"), "agents": agents}
    if slots is not None:
        spec["slots"] = slots
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec))
    return str(path)


def _user_texts(kwargs) -> list[str]:
    return ["".join(p.text for p in m.parts if p.kind == "text") for m in kwargs["messages"] if m.role == "user"]


def _events(out: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in out.getvalue().splitlines()]


def test_run_spec_runs_every_agent_in_one_process_until_each_stops(tmp_path, monkeypatch):
    _agent_dir(tmp_path, monkeypatch)
    room = swarm.Room(tmp_path / "bus")
    turns: dict[str, int] = {"a": 0, "b": 0}

    def stream(**kwargs):
        me = "a" if "a-opener" in _user_texts(kwargs)[0] else "b"
        turns[me] += 1
        if me == "a":
            room.send("a", "b", "ping")                       # b wakes to this
        if sum(turns.values()) == 3:                          # a once, b twice: everyone has worked
            room.send("steer", "*", "enough", kind="stop")
        kwargs["on_text"]("ok")
        return _text("ok")

    monkeypatch.setattr(runtime.model_client, "stream_model_async", stream)
    out = io.StringIO()
    spec = _spec(tmp_path, [{"name": "a", "agent": "voice", "opener": "a-opener", "session": "s-a", "cwd": str(tmp_path)},
                            {"name": "b", "agent": "voice", "opener": "b-opener", "session": "s-b", "cwd": str(tmp_path)}])

    assert swarm_run.run(spec, out=out) == 0

    events = _events(out)
    assert turns == {"a": 1, "b": 2}
    assert all("agent" in e for e in events)
    assert {(e["agent"], e["reason"]) for e in events if e["type"] == "agent_end"} == {("a", "stopped"), ("b", "stopped")}
    assert events[-1]["type"] == "run_end"
    sessions = {e["agent"]: e["session"] for e in events if e["type"] == "agent_start"}
    assert len(memory.load_replay_messages(Path(sessions["b"]))) >= 4   # opener, reply, wake, reply
    assert "ping" in _user_texts_of(memory.load_replay_messages(Path(sessions["b"])))[-1]


def _user_texts_of(messages: list[dict]) -> list[str]:
    return [str(m.get("content") or "") for m in messages if m.get("role") == "user"]


def test_recruit_in_process_adds_an_agent_to_the_same_loop(tmp_path, monkeypatch):
    _agent_dir(tmp_path, monkeypatch)
    room = swarm.Room(tmp_path / "bus")
    seen: list[str] = []

    def stream(**kwargs):
        texts = _user_texts(kwargs)
        me = "twig" if "twig-opener" in texts[0] else "a"
        seen.append(me)
        if me == "a" and seen.count("a") == 1:
            args = json.dumps({"name": "twig", "opener": "twig-opener"})
            message = ai.types.messages.Message(role="assistant", parts=[
                ai.types.messages.ToolCallPart(tool_call_id="c1", tool_name="recruit", tool_args=args)])
            return ModelStreamResult(text="", tool_calls=[ModelToolCall(id="c1", name="recruit", arguments=args)],
                                     reasoning="", usage=ai.types.usage.Usage(input_tokens=1, output_tokens=1),
                                     finish_reason="tool_calls", assistant_message=message)
        if me == "twig":
            room.send("steer", "*", "enough", kind="stop")
        kwargs["on_text"]("ok")
        return _text("ok")

    monkeypatch.setattr(runtime.model_client, "stream_model_async", stream)
    out = io.StringIO()
    spec = _spec(tmp_path, [{"name": "a", "agent": "voice", "opener": "a-opener", "session": "s-a", "cwd": str(tmp_path)}])

    assert swarm_run.run(spec, out=out) == 0

    events = _events(out)
    starts = {e["agent"]: e for e in events if e["type"] == "agent_start"}
    assert set(starts) == {"a", "twig"}
    assert starts["twig"]["session"].endswith("s-a-twig.jsonl")
    assert "twig" in seen
    assert json.loads((tmp_path / "bus" / "twig" / "spawned").read_text())["by"] == "a"
    assert {e["agent"] for e in events if e["type"] == "agent_end"} == {"a", "twig"}


def test_slots_cap_how_many_agents_are_mid_turn_at_once(tmp_path, monkeypatch):
    _agent_dir(tmp_path, monkeypatch)
    room = swarm.Room(tmp_path / "bus")
    busy = {"now": 0, "peak": 0, "turns": 0}

    async def stream(**kwargs):
        busy["now"] += 1
        busy["peak"] = max(busy["peak"], busy["now"])
        await asyncio.sleep(0.2)
        busy["now"] -= 1
        busy["turns"] += 1
        if busy["turns"] == 2:
            room.send("steer", "*", "enough", kind="stop")
        kwargs["on_text"]("ok")
        return _text("ok")

    monkeypatch.setattr(runtime.model_client, "stream_model_async", stream)
    agents = [{"name": n, "agent": "voice", "opener": f"{n}-opener", "session": f"s-{n}", "cwd": str(tmp_path)}
              for n in ("a", "b")]

    assert swarm_run.run(_spec(tmp_path, agents, slots=1), out=io.StringIO()) == 0
    assert busy["peak"] == 1

    busy.update(now=0, peak=0, turns=0)
    for n in ("a", "b"):
        swarm.Room(tmp_path / "bus").leave(n)
    assert swarm_run.run(_spec(tmp_path, agents, slots=0), out=io.StringIO()) == 0
    assert busy["peak"] == 2


def test_compaction_runs_before_a_swarm_agent_sleeps(tmp_path, monkeypatch):
    _agent_dir(tmp_path, monkeypatch)
    room = swarm.Room(tmp_path / "bus")
    timeline: list[str] = []

    def stream(**kwargs):
        timeline.append("turn")
        if timeline.count("turn") == 1:
            threading.Timer(0.3, lambda: room.send("b", "a", "ping")).start()
        else:
            room.send("steer", "a", "enough", kind="stop")
        kwargs["on_text"]("ok")
        return _text("ok")

    monkeypatch.setattr(runtime.model_client, "stream_model_async", stream)
    monkeypatch.setattr(cli, "_maybe_auto_compact", lambda cfg, state: timeline.append("compact"))

    code = cli._run_prompt("go", session="s1", swarm=str(tmp_path / "bus" / "a"),
                           events=headless.JsonEvents(io.StringIO()))

    assert code == 0
    assert timeline.count("turn") == 2
    assert timeline[:4] == ["turn", "compact", "turn", "compact"]  # the trigger ran before each sleep
