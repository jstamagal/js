"""The swarm bus (js.swarm): inboxes, delivery at tool boundaries, sleep and
wake; and the runner (js.swarm_run) that runs every agent in one process."""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import threading
import time
from pathlib import Path

import ai
import ai.types.usage
import pytest
from ai.providers import history_utils
from turn_driver import one_turn

from js import memory, runtime, swarm, swarm_run
from js.config import Config
from js.conversation import Conversation
from js.model_client import ModelStreamResult, ModelToolCall
from js.toolkit import ToolContext, bus_tools
from js.toolkit.core import Tool, call_tool
from js.toolkit.registry import ToolRegistry

# --------------------------------------------------------------------------
# the bus
# --------------------------------------------------------------------------

def _bus(name: str, context, **args):
    """What the bus tool ``name`` answers a call with ``args``."""
    tool = {tool.name: tool for tool in bus_tools.tools()}[name]
    return call_tool(tool, args, context)


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


def test_a_rollcall_is_answered_by_the_process_and_touches_no_inbox_or_log(tmp_path, monkeypatch):
    monkeypatch.setattr(swarm, "PULSE_S", 0.01)
    agent = swarm.Agent(tmp_path / "bus" / "a")
    answers = agent.room.rollcall(wait=5)
    assert answers["a"]["pid"] == os.getpid() and answers["a"]["state"] == "working"
    assert not agent.inbox.pending() and not agent.room.log.exists()


def test_a_member_with_no_process_is_silent_at_rollcall_and_who_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(swarm, "PULSE_S", 0.01)
    monkeypatch.setattr(swarm, "ROLLCALL_WAIT_S", 0.0)
    here = swarm.Agent(tmp_path / "bus" / "here")
    here.room.join("gone")
    assert here.room.rollcall(wait=0.5)["gone"] is None
    lines = dict(line.split(" ", 1) for line in swarm.roster(here.room).splitlines())
    assert "no answer" in lines["gone"] and "no answer" not in lines["here"]


def test_a_retired_agent_is_not_called_at_rollcall(tmp_path, monkeypatch):
    monkeypatch.setattr(swarm, "PULSE_S", 0.01)
    agent = swarm.Agent(tmp_path / "bus" / "a")
    agent.retire("done")
    assert agent.room.rollcall(wait=0.1) == {}


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

    one_turn(_cfg(tmp_path), "SYS", messages, runtime.Telemetry(debug_log=None),
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
    registry = bus_tools.with_bus_tools(ToolRegistry(tools=(), aliases={}))
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
    assert [m.body for m in asyncio.run(agent.sleep_async())] == ["one", "two"]


def test_on_a_bus_with_a_hold_broadcasts_pile_up_into_one_wake_and_a_message_by_name_wakes_at_once(tmp_path):
    agent = swarm.Agent(tmp_path / "bus" / "a")
    agent.room.set_hold(1.0)
    agent.room.send("b", "*", "one")
    threading.Timer(0.4, agent.room.send, ("b", "*", "two")).start()
    started = time.monotonic()
    assert [m.body for m in asyncio.run(agent.sleep_async())] == ["one", "two"]
    assert time.monotonic() - started >= 1.0
    agent.room.send("b", "*", "three")
    agent.room.send("b", "a", "for you")
    started = time.monotonic()
    assert [m.body for m in asyncio.run(agent.sleep_async())] == ["three", "for you"]
    assert time.monotonic() - started < 0.9


def test_a_second_wake_me_leaves_the_pending_alarm_where_it_was(tmp_path):
    agent = swarm.Agent(tmp_path / "bus" / "a")
    context = ToolContext(cwd=tmp_path)
    context.swarm = agent
    _bus("wake_me", context, seconds=300)
    alarm = agent.alarm
    _bus("wake_me", context, seconds=600)
    assert agent.alarm == alarm


def test_wake_me_wakes_the_agent_with_a_tick_when_nothing_lands(tmp_path):
    agent = swarm.Agent(tmp_path / "bus" / "a")
    agent.wake_me(0.3)
    started = time.monotonic()
    landed = asyncio.run(agent.sleep_async())
    assert [(m.sender, m.kind) for m in landed] == [(swarm.CLOCK, swarm.TICK)]
    assert 0.2 < time.monotonic() - started < 3
    assert agent.alarm is None


def test_quiet_means_every_member_asleep_with_an_empty_inbox(tmp_path):
    room = swarm.Room(tmp_path / "bus")
    assert room.quiet() is False  # nobody on the bus
    a, b = swarm.Agent(tmp_path / "bus" / "a"), swarm.Agent(tmp_path / "bus" / "b")
    assert room.quiet() is False  # both working
    sleepers = [threading.Thread(target=lambda x=x: asyncio.run(x.sleep_async()), daemon=True) for x in (a, b)]
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
    retire = bus_tools.with_bus_tools(ToolRegistry(tools=(), aliases={})).resolve("retire")
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


def test_concurrent_subscriptions_preserve_all_kinds(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    room = swarm.Room(tmp_path / "bus")
    start = threading.Barrier(16)

    def subscribe(index):
        start.wait(timeout=5)
        for number in range(10):
            room.subscribe("a", f"kind-{index}-{number}")

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(subscribe, range(16)))
    assert room.subs_of("a") == {f"kind-{i}-{n}" for i in range(16) for n in range(10)}


def test_subscription_readers_see_complete_snapshot(tmp_path, monkeypatch):
    room = swarm.Room(tmp_path / "bus")
    room.subscribe("a", "original")
    write_text = Path.write_text
    observations = []

    def interrupted_write(path, text, *args, **kwargs):
        write_text(path, text[:1], *args, **kwargs)
        observations.append(room.subs_of("a"))
        return write_text(path, text, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", interrupted_write)
    room.subscribe("a", "second")
    assert observations == [{"original"}]
    assert room.subs_of("a") == {"original", "second"}
    room.subscribe("a", "original", on=False)
    assert room.subs_of("a") == {"second"}


@pytest.mark.parametrize("case", ["other_holder", "expired", "untaken", "done"])
def test_finish_requires_live_holder_and_unfinished_task(tmp_path, monkeypatch, case):
    agent = swarm.Agent(tmp_path / "bus" / "a")
    room = agent.room
    cell = room.post_cell("a", "work")
    if case != "untaken":
        room.take_cell(cell.id, "b" if case == "other_holder" else "a", ttl=1)
    if case == "expired":
        now = time.time()
        monkeypatch.setattr(swarm.time, "time", lambda: now + 2)
    if case == "done":
        room.finish_cell(cell.id, "a", "original")
    before_cells = (room.root / "cells.json").read_bytes()
    before_claims = room.claims()
    before_log = room.log.read_bytes()
    response = _bus("finish_task", ToolContext(cwd=tmp_path, swarm=agent), id=cell.id, result="replacement")
    assert response.startswith("ERROR")
    assert (room.root / "cells.json").read_bytes() == before_cells
    assert room.claims() == before_claims
    assert room.log.read_bytes() == before_log


def test_task_tools_work_the_board_from_the_agent_on_the_context(tmp_path):
    a = swarm.Agent(tmp_path / "bus" / "a")
    registry = bus_tools.with_bus_tools(ToolRegistry(tools=(), aliases={}))
    post, take, finish, tasks = (registry.resolve(n) for n in ("post_task", "take_task", "finish_task", "tasks"))
    on_bus = ToolContext(cwd=tmp_path, swarm=a)
    assert post.handler(title="write the parser", body="in src", context=on_bus).startswith("posted task #1")
    assert "write the parser" in take.handler(id=1, context=on_bus)
    assert take.handler(id=7, context=on_bus).startswith("no task #7")
    assert finish.handler(id=1, result="done on feat/parser", context=on_bus).startswith("task #1 done")
    assert "done by a" in tasks.handler(context=on_bus)


# --------------------------------------------------------------------------
# headless: a turn ends, the agent sleeps, a message wakes it
# --------------------------------------------------------------------------

def _agent_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JS_DEFAULT_AGENT", "voice")
    monkeypatch.setenv("JS_MODEL", "offline-test-model")
    monkeypatch.setattr(runtime.T, "STOCK_CONTEXT", ToolContext(cwd=tmp_path))
    directory = tmp_path / ".js" / "agents" / "voice"
    directory.mkdir(parents=True)
    (directory / "00-role.md").write_text("SYSTEM", encoding="utf-8")


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


def test_swarm_runner_fails_when_an_agent_cannot_be_configured(tmp_path, monkeypatch):
    def configure(spec):
        raise ValueError("invalid agent configuration")

    monkeypatch.setattr(swarm_run, "_configure", configure)
    out = io.StringIO()
    spec = _spec(tmp_path, [{"name": "a", "opener": "go", "cwd": str(tmp_path)}])

    assert swarm_run.run(spec, out=out) == 1
    events = _events(out)
    assert [(e["agent"], e["reason"]) for e in events if e["type"] == "agent_end"] == [("a", "error")]
    assert events[-1]["exit_code"] == 1


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

    async def compact(self, **_kwargs):
        timeline.append("compact")
        return False

    monkeypatch.setattr(runtime.model_client, "stream_model_async", stream)
    monkeypatch.setattr(Conversation, "maybe_compact", compact)
    spec = _spec(tmp_path, [{"name": "a", "agent": "voice", "opener": "go", "session": "s-a", "cwd": str(tmp_path)}])

    assert swarm_run.run(spec, out=io.StringIO()) == 0
    assert timeline == ["turn", "compact", "turn", "compact"]  # the trigger ran before each sleep


def test_recruit_past_the_spawn_cap_starts_no_agent(tmp_path, monkeypatch):
    _agent_dir(tmp_path, monkeypatch)
    monkeypatch.setenv("JS_SWARM_SPAWN_MAX", "0")
    room = swarm.Room(tmp_path / "bus")
    calls = 0

    def stream(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            args = json.dumps({"name": "twig", "opener": "twig-opener"})
            message = ai.types.messages.Message(role="assistant", parts=[
                ai.types.messages.ToolCallPart(tool_call_id="c1", tool_name="recruit", tool_args=args)])
            return ModelStreamResult(text="", tool_calls=[ModelToolCall(id="c1", name="recruit", arguments=args)],
                                     reasoning="", usage=ai.types.usage.Usage(input_tokens=1, output_tokens=1),
                                     finish_reason="tool_calls", assistant_message=message)
        room.send("steer", "*", "enough", kind="stop")
        kwargs["on_text"]("ok")
        return _text("ok")

    monkeypatch.setattr(runtime.model_client, "stream_model_async", stream)
    out = io.StringIO()
    spec = _spec(tmp_path, [{"name": "a", "agent": "voice", "opener": "a-opener", "session": "s-a", "cwd": str(tmp_path)}])

    assert swarm_run.run(spec, out=out) == 0
    assert calls == 2
    assert {e["agent"] for e in _events(out) if e["type"] == "agent_start"} == {"a"}
    assert not (tmp_path / "bus" / "twig").exists()


def test_swarm_agent_turn_is_on_disk_before_it_ends(tmp_path, monkeypatch):
    from js.memory import load_messages

    _agent_dir(tmp_path, monkeypatch)
    room = swarm.Room(tmp_path / "bus")
    on_disk: list[list[str]] = []

    async def turn(cfg, system, messages, telemetry, *, checkpoint, **kwargs):
        messages.append({"role": "assistant", "content": "", "tool_calls": [{
            "id": "read-1", "type": "function", "function": {"name": "read", "arguments": "{}"}}]})
        messages.append({"role": "tool", "tool_call_id": "read-1", "name": "read", "content": "found"})
        checkpoint()
        # What a kill here would leave behind.
        on_disk.append([m["role"] for m in load_messages(cfg.session_file)])
        messages.append({"role": "assistant", "content": "done"})
        room.send("steer", "*", "enough", kind="stop")

    monkeypatch.setattr(runtime, "run_turn_async", turn)
    spec = _spec(tmp_path, [{"name": "a", "agent": "voice", "opener": "go", "session": "s-a", "cwd": str(tmp_path)}])
    assert swarm_run.run(spec, out=io.StringIO()) == 0
    assert on_disk == [["user", "assistant", "tool"]]


def test_swarm_runner_accepts_agent_configuration_service(tmp_path):
    configured = []
    def configure(spec):
        configured.append(spec.name)
        raise ValueError("configuration refused")
    spec = _spec(tmp_path, [{"name": "a", "opener": "go", "cwd": str(tmp_path)}])
    assert swarm_run.run(spec, out=io.StringIO(), configure=configure) == 1
    assert configured == ["a"]


def test_bus_send_hands_its_message_to_the_result_event(tmp_path, capsys):
    from js.toolkit.core import call_scope, call_view

    agent = swarm.Agent(tmp_path / "bus" / "a")
    context = ToolContext(cwd=tmp_path, swarm=agent)
    with call_scope("send-1"):
        _bus("send", context, to="b", text="inspect the parser")
        message = call_view()["message"]

    assert message["from"] == "a"
    assert message["to"] == "b"
    assert message["body"] == "inspect the parser"
    assert message["seq"] == 1
    assert capsys.readouterr() == ("", "")


def test_cancel_ends_the_turn_of_an_agent_that_ignores_stop(tmp_path, monkeypatch):
    _agent_dir(tmp_path, monkeypatch)
    room = swarm.Room(tmp_path / "bus")
    calls: list[str] = []

    async def stream(**kwargs):
        calls.append(_user_texts(kwargs)[-1])
        # A turn that never reaches a tool boundary never reads the stop.
        room.send("steer", "a", "wrap up", kind=swarm.STOP)
        room.send("steer", "a", "", kind=swarm.CANCEL)
        await asyncio.Event().wait()

    monkeypatch.setattr(runtime.model_client, "stream_model_async", stream)
    out = io.StringIO()
    spec = _spec(tmp_path, [{"name": "a", "agent": "voice", "opener": "a-opener", "session": "s-a", "cwd": str(tmp_path)}])

    assert swarm_run.run(spec, out=out) == 0

    events = _events(out)
    assert len(calls) == 1
    assert [e["type"] for e in events if e["type"] in ("turn_cancelled", "agent_end")] == ["turn_cancelled", "agent_end"]
    assert [e["reason"] for e in events if e["type"] == "agent_end"] == ["stopped"]
    session = next(e["session"] for e in events if e["type"] == "agent_start")
    assert "a-opener" in _user_texts_of(memory.load_replay_messages(Path(session)))[0]


def test_restart_runs_an_ended_agent_again_on_its_session(tmp_path, monkeypatch):
    _agent_dir(tmp_path, monkeypatch)
    room = swarm.Room(tmp_path / "bus")
    out = io.StringIO()
    seen: list[str] = []

    def ended(name: str) -> bool:
        return any(e["type"] == "agent_end" and e["agent"] == name for e in _events(out))

    async def stream(**kwargs):
        texts = _user_texts(kwargs)
        seen.append(texts[-1])
        if "a-opener" in texts[-1]:
            room.send("steer", "a", "done", kind=swarm.STOP)
        elif "b-opener" in texts[-1]:
            # b stays on the bus until a has ended, then brings it back.
            while not ended("a"):
                await asyncio.sleep(0.05)
            room.send("steer", "a", "come back", kind=swarm.RESTART)
        elif "come back" in texts[-1]:
            room.send("steer", "*", "enough", kind=swarm.STOP)
        kwargs["on_text"]("ok")
        return _text("ok")

    monkeypatch.setattr(runtime.model_client, "stream_model_async", stream)
    spec = _spec(tmp_path, [{"name": "a", "agent": "voice", "opener": "a-opener", "cwd": str(tmp_path)},
                            {"name": "b", "agent": "voice", "opener": "b-opener", "session": "s-b", "cwd": str(tmp_path)}])

    assert swarm_run.run(spec, out=out) == 0

    events = _events(out)
    starts = [e for e in events if e["type"] == "agent_start" and e["agent"] == "a"]
    assert len(starts) == 2
    assert starts[0]["session"] == starts[1]["session"]
    assert any(e["type"] == "restart" and e["agent"] == "a" for e in events)
    texts = _user_texts_of(memory.load_replay_messages(Path(starts[1]["session"])))
    assert "a-opener" in texts[0] and "come back" in texts[-1]


def test_a_runner_message_for_an_agent_not_in_that_state_is_dropped(tmp_path, monkeypatch):
    _agent_dir(tmp_path, monkeypatch)
    room = swarm.Room(tmp_path / "bus")
    out = io.StringIO()

    async def stream(**kwargs):
        room.send("steer", "a", "", kind=swarm.RESTART)   # a is running, not ended
        while not any(e["type"] == "dropped" for e in _events(out)):
            await asyncio.sleep(0.05)
        room.send("steer", "a", "enough", kind=swarm.STOP)
        kwargs["on_text"]("ok")
        return _text("ok")

    monkeypatch.setattr(runtime.model_client, "stream_model_async", stream)
    spec = _spec(tmp_path, [{"name": "a", "agent": "voice", "opener": "a-opener", "session": "s-a", "cwd": str(tmp_path)}])

    assert swarm_run.run(spec, out=out) == 0

    events = _events(out)
    assert [(e["agent"], e["message_kind"]) for e in events if e["type"] == "dropped"] == [("a", "restart")]
    assert len([e for e in events if e["type"] == "agent_start"]) == 1


class _Kernel:
    def __init__(self):
        self.shut = 0

    def shutdown(self):
        self.shut += 1


def test_an_ended_swarm_agent_shuts_its_kernel_down(tmp_path, monkeypatch):
    _agent_dir(tmp_path, monkeypatch)
    room = swarm.Room(tmp_path / "bus")
    kernel = _Kernel()
    real_turn = runtime.run_turn_async

    async def turn_with_kernel(*args, **kwargs):
        kwargs["tool_context"].kernel_session = kernel
        return await real_turn(*args, **kwargs)

    def stream(**kwargs):
        room.send("steer", "a", "done", kind=swarm.STOP)
        kwargs["on_text"]("ok")
        return _text("ok")

    monkeypatch.setattr(runtime, "run_turn_async", turn_with_kernel)
    monkeypatch.setattr(runtime.model_client, "stream_model_async", stream)
    spec = _spec(tmp_path, [{"name": "a", "agent": "voice", "opener": "go", "session": "s-a", "cwd": str(tmp_path)}])

    assert swarm_run.run(spec, out=io.StringIO()) == 0
    assert kernel.shut == 1


def _project_jsrc(tmp_path, text: str) -> None:
    (tmp_path / ".js").mkdir(exist_ok=True)
    (tmp_path / ".js" / "jsrc").write_text(text, encoding="utf-8")


def test_a_jsrc_tool_call_guard_vets_a_swarm_agents_calls(tmp_path, monkeypatch):
    _agent_dir(tmp_path, monkeypatch)
    monkeypatch.setenv("SHELL", "/bin/sh")
    _project_jsrc(tmp_path, "on tool_call exec sh -c 'echo off limits >&2; exit 2'\n")
    room = swarm.Room(tmp_path / "bus")
    calls = iter([_call("who", "c1")])

    def stream(**kwargs):
        reply = next(calls, None)
        if reply is not None:
            return reply
        room.send("steer", "a", "done", kind=swarm.STOP)
        kwargs["on_text"]("ok")
        return _text("ok")

    monkeypatch.setattr(runtime.model_client, "stream_model_async", stream)
    out = io.StringIO()
    spec = _spec(tmp_path, [{"name": "a", "agent": "voice", "opener": "go", "session": "s-a", "cwd": str(tmp_path)}])

    assert swarm_run.run(spec, out=out) == 0

    session = next(e["session"] for e in _events(out) if e["type"] == "agent_start")
    results = [m for m in memory.load_replay_messages(Path(session)) if m.get("role") == "tool"]
    assert results and str(results[0]["content"]).startswith("ERROR: off limits")


def test_a_jsrc_handler_that_needs_the_repl_is_reported_once_and_not_run(tmp_path, monkeypatch, capsys):
    _agent_dir(tmp_path, monkeypatch)
    _project_jsrc(tmp_path, "on turn_start set READ_LINES_MAX 5\n")
    room = swarm.Room(tmp_path / "bus")

    def stream(**kwargs):
        room.send("steer", "*", "done", kind=swarm.STOP)
        kwargs["on_text"]("ok")
        return _text("ok")

    monkeypatch.setattr(runtime.model_client, "stream_model_async", stream)
    out = io.StringIO()
    spec = _spec(tmp_path, [{"name": n, "agent": "voice", "opener": "go", "session": f"s-{n}", "cwd": str(tmp_path)}
                            for n in ("a", "b")])

    assert swarm_run.run(spec, out=out) == 0

    assert capsys.readouterr().err.count(f"{tmp_path / '.js' / 'jsrc'}:1") == 1
    assert all(json.loads(line) for line in out.getvalue().splitlines())


def test_a_second_agent_on_a_running_agents_session_fails_without_writing(tmp_path, monkeypatch):
    _agent_dir(tmp_path, monkeypatch)
    room = swarm.Room(tmp_path / "bus")
    out = io.StringIO()
    turns: list[str] = []

    async def stream(**kwargs):
        turns.append(_user_texts(kwargs)[-1])
        # The first agent keeps its session until the second has ended.
        for _ in range(100):
            if any(e["type"] == "agent_end" for e in _events(out)):
                break
            await asyncio.sleep(0.05)
        room.send("steer", "*", "done", kind=swarm.STOP)
        kwargs["on_text"]("ok")
        return _text("ok")

    monkeypatch.setattr(runtime.model_client, "stream_model_async", stream)
    spec = _spec(tmp_path, [{"name": n, "agent": "voice", "opener": f"{n}-opener", "session": "shared", "cwd": str(tmp_path)}
                            for n in ("a", "b")])

    assert swarm_run.run(spec, out=out) == 1

    events = _events(out)
    ends = {e["agent"]: e["reason"] for e in events if e["type"] == "agent_end"}
    assert sorted(ends.values()) == ["error", "stopped"]
    assert len(turns) == 1


def test_what_an_agent_writes_to_stderr_is_tagged_and_kept_in_its_own_file(tmp_path, monkeypatch, capsys):
    _agent_dir(tmp_path, monkeypatch)
    room = swarm.Room(tmp_path / "bus")

    def stream(**kwargs):
        me = "a" if "a-opener" in _user_texts(kwargs)[0] else "b"
        print(f"trouble in {me}", file=sys.stderr)
        room.send("steer", me, "done", kind=swarm.STOP)
        kwargs["on_text"]("ok")
        return _text("ok")

    monkeypatch.setattr(runtime.model_client, "stream_model_async", stream)
    spec = _spec(tmp_path, [{"name": n, "agent": "voice", "opener": f"{n}-opener", "session": f"s-{n}",
                             "cwd": str(tmp_path)} for n in ("a", "b")])

    assert swarm_run.run(spec, out=io.StringIO()) == 0

    err = capsys.readouterr().err.splitlines()
    assert "[a] trouble in a" in err and "[b] trouble in b" in err
    for name in ("a", "b"):
        assert (tmp_path / "bus" / name / "stderr").read_text(encoding="utf-8") == f"trouble in {name}\n"
