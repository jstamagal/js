"""A subagent's launch and end: one subagent_start and one subagent_end event
carrying the child's own model, effort, tools and session, drawn as one line
each in the REPL's scrollback and on -p's stderr."""

from __future__ import annotations

import asyncio
import io
from dataclasses import replace

import pytest

from js import agent_messages, console, logins, memory, output, runtime
from js.config import from_env
from js.logins import Login
from js.toolkit import ToolContext, meta
from js.toolkit.registry import build_default_registry


class _Events:
    def __init__(self) -> None:
        self.seen: list[output.OutputEvent] = []

    def emit(self, event: output.OutputEvent) -> None:
        self.seen.append(event)

    def named(self, name: str) -> list[dict]:
        return [dict(event.fields) for event in self.seen if event.name == name]


def _agents(tmp_path, worker_yaml: str):
    root = tmp_path / "agents"
    for name, yaml in (("parent", "tools: []\n"), ("worker", worker_yaml)):
        (root / name).mkdir(parents=True)
        (root / name / "agent.yaml").write_text(yaml, encoding="utf-8")
        (root / name / "01-prompt.md").write_text("SYSTEM\n", encoding="utf-8")
    return root


@pytest.fixture
def parent_cfg(monkeypatch, tmp_path):
    for name in ("JS_MODEL", "JS_PROVIDER", "JS_BASE_URL", "JS_API_KEY", "JS_EFFORT"):
        monkeypatch.delenv(name, raising=False)
    logins.save_login(Login(provider_id="testes", sdk_provider_id="openai", provider_base_url="http://127.0.0.1:9",
                            provider_api_key="x"))
    monkeypatch.setenv("JS_MODEL", "testes/parent-model")

    def build(worker_yaml: str):
        root = _agents(tmp_path, worker_yaml)
        return replace(from_env(cwd=tmp_path, save_session=False), prompts_dir=root / "parent",
                       prompt_roots=(root,), session_file=tmp_path / "parent.jsonl", reasoning_effort="low")
    return build


@pytest.fixture
def child_turn(monkeypatch):
    seen: dict = {}

    async def turn(child_cfg, system, messages, telemetry, **kwargs):
        seen["cfg"] = child_cfg
        kwargs["call_stats"].append({"prompt_tokens": 1200, "output_tokens": 34})
        messages.append({"role": "assistant", "content": "done"})

    async def no_init(*args, **kwargs):
        return None

    monkeypatch.setattr(runtime, "run_turn_async", turn)
    monkeypatch.setattr(agent_messages, "initialize", no_init)
    monkeypatch.setattr(memory, "persist_messages", lambda *a, **k: None)
    return seen


def _run(cfg, tmp_path, *, total=1, idx=1):
    events = _Events()

    async def go():
        with output.using_sink(events):
            return await meta._run_one_task_async(
                idx, total, "work", ToolContext(cwd=tmp_path), cfg, build_default_registry(), "worker", None)

    return asyncio.run(go()), events


def test_the_start_event_carries_the_childs_own_model_effort_and_tools(parent_cfg, child_turn, tmp_path):
    cfg = parent_cfg("model: testes/child-model\nreasoning: high\ntools:\n  - read:eager\n  - shell:eager\n")

    result, events = _run(cfg, tmp_path, idx=2, total=3)

    assert result == "done"
    (start,) = events.named("subagent_start")
    assert start["agent"] == "worker"
    assert (start["index"], start["total"]) == (2, 3)
    assert start["model"] == child_turn["cfg"].model == "child-model"
    assert start["provider"] == "testes"
    assert start["route"] == "testes/child-model"
    assert start["effort"] == "high"
    assert start["tools"] == 2
    assert start["session_file"] == str(child_turn["cfg"].session_file)
    assert start["session"] == child_turn["cfg"].session_file.stem


def test_a_child_without_its_own_model_shows_the_parents(parent_cfg, child_turn, tmp_path):
    cfg = parent_cfg("tools: []\n")

    _result, events = _run(cfg, tmp_path)

    (start,) = events.named("subagent_start")
    assert start["route"] == "testes/parent-model"
    assert start["effort"] == "low"
    assert start["tools"] == 0


def test_the_end_event_says_ok_and_sums_the_childs_tokens(parent_cfg, child_turn, tmp_path):
    _result, events = _run(parent_cfg("tools: []\n"), tmp_path)

    (end,) = events.named("subagent_end")
    assert end["outcome"] == "ok"
    assert (end["prompt_tokens"], end["output_tokens"], end["calls"]) == (1200, 34, 1)
    assert end["elapsed_s"] >= 0


def test_a_child_turn_that_raises_ends_as_an_error(parent_cfg, monkeypatch, tmp_path):
    async def failing(*args, **kwargs):
        raise RuntimeError("boom")

    async def no_init(*args, **kwargs):
        return None

    monkeypatch.setattr(runtime, "run_turn_async", failing)
    monkeypatch.setattr(agent_messages, "initialize", no_init)
    monkeypatch.setattr(memory, "persist_messages", lambda *a, **k: None)

    result, events = _run(parent_cfg("tools: []\n"), tmp_path)

    assert result.startswith("ERROR")
    (end,) = events.named("subagent_end")
    assert end["outcome"] == "error"


@pytest.mark.parametrize(("handler", "kind"), [("task", "task"), ("named", "agent"), ("background", "background")])
def test_the_launch_kind_names_how_the_subagent_was_started(monkeypatch, handler, kind):
    from js.toolkit import task_jobs

    kinds = []

    async def probe(idx, total, item, context, parent_cfg, full_registry, agent_id, session_id, model=""):
        kinds.append(meta._LAUNCH_KIND.get())
        return "ok"

    monkeypatch.setattr(meta, "_run_one_task_async", probe)
    monkeypatch.setattr(task_jobs, "_JOBS", {})
    context = ToolContext()
    context.config = from_env(save_session=False)

    async def go():
        if handler == "named":
            tool = meta.named_agent_tool("worker")
            return await meta.dispatch_fan_out_async(tool, {"tasks": ["t"]}, context)
        if handler == "background":
            text = await meta.task_async(tasks=["t"], agent_id="worker", background=True, context=context)
            for _ in range(50):
                if kinds:
                    break
                await asyncio.sleep(0.01)
            return text
        return await meta.task_async(tasks=["t"], agent_id="worker", context=context)

    asyncio.run(go())
    assert kinds == [kind]


def test_the_console_draws_one_line_for_each_on_stderr(monkeypatch):
    err = io.StringIO()
    monkeypatch.setattr("sys.stderr", err)
    sink = console.ConsoleSink(None)
    launch = {"agent": "worker", "kind": "agent", "index": 1, "total": 2, "model": "m", "provider": "p",
              "route": "p/m", "effort": "high", "tools": 7, "session": "task-1", "session_file": "/x/task-1.jsonl"}

    sink.emit(output.event("subagent_start", **launch))
    sink.emit(output.event("subagent_end", **launch, outcome="ok", elapsed_s=3.2,
                           prompt_tokens=10, output_tokens=5, calls=1))

    lines = err.getvalue().splitlines()
    assert len(lines) == 2
    for value in ("worker", "p/m", "high", "7", "task-1"):
        assert value in lines[0]
    assert "worker" in lines[1]
