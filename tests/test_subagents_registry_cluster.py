"""Cluster coverage for the subagents/registry fan-out fixes:

- session_id + multiple tasks is rejected cleanly (never silently shares a file).
- subagents never run more workers than the cap, across the whole tree.
- the subagent-model lock strips the `model` override at every depth.
- a typo'd exact tool selector warns instead of silently shrinking the surface.
"""

from __future__ import annotations

import asyncio
import time
import types

import pytest

from js import settings
from js.toolkit import ToolContext, registry as registry_mod, subagents
from js.toolkit.registry import build_default_registry, select

# --------------------------------------------------------------------------
# Item 2 — session_id + multiple tasks
# --------------------------------------------------------------------------

async def _echo(idx, total, item, *args, **kwargs):
    return f"ran {item}"


def _ctx(worker=_echo) -> ToolContext:
    """A context whose subagents are ``worker``, called as the task tool calls
    a subagent turn."""
    ctx = ToolContext()
    ctx.config = types.SimpleNamespace(lock_subagent_model=False, prompt_roots=None)
    ctx.subagent_assembly = subagents.SubagentAssembly(worker=worker)
    return ctx


def _task(context: ToolContext, tasks: list[str], session_id: str | None = None) -> str:
    """What the task tool returns for ``tasks`` run as `worker` agents."""
    return asyncio.run(subagents.task_async(tasks=tasks, agent_id="worker", session_id=session_id, context=context))


def test_session_id_with_multiple_tasks_is_rejected():
    error = _task(_ctx(), ["one", "two"], session_id="s1")
    assert error.startswith("ERROR: session_id runs one task per session")


def test_session_id_with_single_task_is_allowed():
    assert _task(_ctx(), ["only"], session_id="s1") == "ran only"


def test_multiple_tasks_without_session_id_is_allowed():
    result = _task(_ctx(), ["one", "two", "three"])
    assert not result.startswith("ERROR")
    assert all(f"ran {item}" in result for item in ("one", "two", "three"))


# --------------------------------------------------------------------------
# Item 3 — width cap on the fan-out
# --------------------------------------------------------------------------

def test_worker_cap_never_exceeds_configured_width():
    peak = 0
    live = 0

    async def probe(idx, total, item, context, parent_cfg, full_registry, agent_id, session_id, model=""):
        nonlocal peak, live
        live += 1
        peak = max(peak, live)
        try:
            await asyncio.sleep(0.02)
        finally:
            live -= 1
        return f"{idx}. done"

    ctx = _ctx(probe)
    ctx.subagent_max_workers = 3
    results = asyncio.run(
        subagents.task_async(tasks=[f"t{n}" for n in range(10)], agent_id="worker", context=ctx)
    )

    assert peak <= 3
    assert "TASK_RESULTS agent=worker" in results
    assert results.count(". done") == 10


def test_worker_cap_defaults_below_the_dispatch_pool():
    peak = 0
    live = 0

    async def probe(idx, total, item, context, parent_cfg, full_registry, agent_id, session_id, model=""):
        nonlocal peak, live
        live += 1
        peak = max(peak, live)
        try:
            await asyncio.sleep(0.02)
        finally:
            live -= 1
        return f"{idx}. done"

    ctx = _ctx(probe)  # no override -> the js/jsrc SUBAGENT_WORKERS_MAX
    asyncio.run(subagents.task_async(tasks=[f"t{n}" for n in range(20)], agent_id="worker", context=ctx))

    assert peak <= settings.default_value("SUBAGENT_WORKERS_MAX")


def _nested_probe(cap: int, depth_max: int, width: int, seen: list[int]):
    """A worker that fans out `width` children until `depth_max`, recording how
    many gate slots are taken while it runs."""

    async def probe(idx, total, item, context, parent_cfg, full_registry, agent_id, session_id, model=""):
        child = ToolContext()
        child.config = context.config
        child.subagent_assembly = context.subagent_assembly
        child.subagent_max_workers = cap
        child.task_max_depth = depth_max
        child.task_depth = getattr(context, "task_depth", 0) + 1
        seen.append(subagents._GATE.running)
        await asyncio.sleep(0.005)
        if child.task_depth < depth_max:
            await subagents.task_async(tasks=[f"t{n}" for n in range(width)], agent_id="worker", context=child)
        seen.append(subagents._GATE.running)
        return "done"

    return probe


def _tree_ctx(cap: int, depth_max: int, worker=_echo) -> ToolContext:
    ctx = _ctx(worker)
    ctx.subagent_max_workers = cap
    ctx.task_max_depth = depth_max
    return ctx


def test_worker_cap_holds_across_the_whole_tree():
    seen: list[int] = []
    context = _tree_ctx(4, 4, _nested_probe(4, 4, 4, seen))

    asyncio.run(subagents.task_async(tasks=[f"t{n}" for n in range(4)], agent_id="worker", context=context))

    # 4 + 16 + 64 + 256 subagents ran, two samples each.
    assert len(seen) == 2 * (4 + 16 + 64 + 256)
    assert max(seen) <= 4
    assert subagents._GATE.running == 0


def test_parent_waiting_on_children_lends_its_slot():
    # One slot, three levels: a parent that kept its slot while waiting would
    # leave its child no slot to run in.
    seen: list[int] = []
    context = _tree_ctx(1, 3, _nested_probe(1, 3, 2, seen))

    async def run():
        return await asyncio.wait_for(
            subagents.task_async(tasks=["a", "b"], agent_id="worker", context=context), 10)

    assert asyncio.run(run()).count("done") == 2
    assert max(seen) <= 1
    assert subagents._GATE.running == 0


def test_cancelled_waiter_gives_back_nothing_it_did_not_take():
    async def run():
        gate = subagents.SubagentGate()
        await gate.acquire(1)
        waiter = asyncio.ensure_future(gate.acquire(1))
        await asyncio.sleep(0)
        waiter.cancel()
        await asyncio.sleep(0)
        gate.release()
        await gate.acquire(1)
        held = gate.running
        gate.release()
        return held, gate.running

    assert asyncio.run(run()) == (1, 0)


def test_a_waiter_on_a_closed_loop_is_not_counted_twice():
    gate = subagents.SubagentGate()
    main = asyncio.new_event_loop()
    other = asyncio.new_event_loop()
    try:
        main.run_until_complete(gate.acquire(1))
        stranded = other.create_task(gate.acquire(1))
        other.run_until_complete(asyncio.sleep(0))
        other.close()

        gate.release()  # wakes the waiter, whose loop is gone
        assert gate.running == 0
        stranded.get_coro().close()  # its coroutine is finalized later
        assert gate.running == 0

        main.run_until_complete(gate.acquire(1))
        assert gate.running == 1
        gate.release()
    finally:
        main.close()
    assert gate.running == 0


def test_a_background_job_that_fails_hands_back_every_slot():
    from js import snippets

    async def probe(idx, total, item, context, parent_cfg, full_registry, agent_id, session_id, model=""):
        if item == "boom":
            await asyncio.sleep(0.05)
            raise snippets.SnippetError("missing snippet")
        await asyncio.sleep(30)
        return "slow"

    ctx = _tree_ctx(2, 3, probe)
    started = subagents.task(tasks=["boom", "slow", "slower"], agent_id="worker", background=True, context=ctx)
    handle = started.rsplit("HANDLE ", 1)[1].split()[0]
    job = subagents.task_jobs.find(handle, ctx.task_owner)
    with pytest.raises(snippets.SnippetError):
        subagents.task_jobs.wait(job, 5)
    deadline = time.monotonic() + 5
    while subagents._GATE.running and time.monotonic() < deadline:
        time.sleep(0.02)

    assert subagents._GATE.running == 0


def test_a_timed_wait_returns_on_time_while_its_job_is_queued():
    waited: list[float] = []

    async def probe(idx, total, item, context, parent_cfg, full_registry, agent_id, session_id, model=""):
        if item == "child":
            await asyncio.sleep(0.5)
            return "child done"
        child = ToolContext()
        child.config = context.config
        child.subagent_assembly = context.subagent_assembly
        child.subagent_max_workers = 1
        child.task_max_depth = 3
        child.task_depth = 1
        started = await subagents.task_async(tasks=["child"], agent_id="worker", background=True, context=child)
        handle = started.rsplit("HANDLE ", 1)[1].split()[0]
        await asyncio.sleep(0.1)  # the child is queued behind this slot
        began = time.monotonic()
        await subagents.task_async(action="wait", handle=handle, timeout=0, context=child)
        waited.append(time.monotonic() - began)
        await subagents.task_async(action="wait", handle=handle, context=child)
        return "parent done"

    result = asyncio.run(asyncio.wait_for(
        subagents.task_async(tasks=["parent"], agent_id="worker", context=_tree_ctx(1, 3, probe)), 10))

    assert result == "parent done"
    assert waited and waited[0] < 0.3
    assert subagents._GATE.running == 0


# --------------------------------------------------------------------------
# Item 4 — nested registry honors the subagent-model lock
# --------------------------------------------------------------------------

def _capture_flags(monkeypatch) -> list[tuple[str, ...]]:
    seen: list[tuple[str, ...]] = []
    real = registry_mod.build_default_registry

    def spy(prompts_root=None, flags=("model_override",)):
        seen.append(tuple(flags))
        return real(prompts_root, flags=flags)

    monkeypatch.setattr(registry_mod, "build_default_registry", spy)
    return seen


def test_locked_subagent_model_strips_override_flag(monkeypatch):
    seen = _capture_flags(monkeypatch)
    ctx = _ctx()
    ctx.config = types.SimpleNamespace(lock_subagent_model=True, prompt_roots=None)

    assert _task(ctx, ["do it"]) == "ran do it"
    assert seen == [()]


def test_unlocked_subagent_model_keeps_override_flag(monkeypatch):
    seen = _capture_flags(monkeypatch)
    assert _task(_ctx(), ["do it"]) == "ran do it"
    assert seen == [("model_override",)]


# --------------------------------------------------------------------------
# Item 5 — typo'd exact selector warns; glob misses stay silent
# --------------------------------------------------------------------------

def test_exact_selector_miss_warns_on_stderr(capsys):
    result = select(["reed:lazy"])
    assert [tool.name for tool in result.tools] == []
    err = capsys.readouterr().err
    assert "reed" in err


def test_exact_selector_miss_names_the_agent(capsys):
    full = build_default_registry()
    full.select(["fs_read:lazy"], agent_id="myagent")
    err = capsys.readouterr().err
    assert "fs_read" in err
    assert "myagent" in err


def test_glob_selector_miss_stays_silent(capsys):
    result = select(["zzz_*:lazy"])
    assert [tool.name for tool in result.tools] == []
    assert capsys.readouterr().err == ""


def test_metachar_selector_is_treated_as_glob(capsys):
    # `?` / `[` with no `*` must route through the (silent) glob branch, not the
    # exact-lookup warn branch.
    result = select(["read?:lazy"])
    assert [tool.name for tool in result.tools] == []
    assert capsys.readouterr().err == ""


def test_good_exact_and_glob_selectors_still_resolve(capsys):
    names = [tool.name for tool in select(["read:lazy", "wiki_*:lazy"]).tools]
    assert "read" in names
    assert "wiki_write" in names and "wiki_convert" in names
    assert capsys.readouterr().err == ""


def test_registry_accepts_assembly_suites_without_stock_agents():
    from js.toolkit import Tool
    from js.toolkit.registry import build_default_registry

    tool = Tool("custom", "custom tool", lambda **kwargs: "ok", {})
    registry = build_default_registry(prompts_root=(), suites=(("assembly", (tool,)),))
    assert tuple(registry.by_name) == ("custom",)
    assert registry.resolve("custom").source == "assembly"


def test_fan_out_accepts_assembly_services():
    context = ToolContext()
    cfg = types.SimpleNamespace(lock_subagent_model=False, prompt_roots=())
    registry = object()
    seen = []
    async def worker(idx, total, item, parent, parent_cfg, full_registry, *args):
        seen.append((parent, parent_cfg, full_registry))
        return item.upper()
    context.subagent_assembly = subagents.SubagentAssembly(
        config_factory=lambda **kwargs: cfg,
        registry_factory=lambda *args, **kwargs: registry,
        worker=worker,
    )
    assert _task(context, ["work"]) == "WORK"
    assert seen == [(context, cfg, registry)]
