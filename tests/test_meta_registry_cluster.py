"""Cluster coverage for the meta/registry fan-out fixes:

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
from js.toolkit import ToolContext
from js.toolkit import meta
from js.toolkit import registry as registry_mod
from js.toolkit.registry import build_default_registry, select


# --------------------------------------------------------------------------
# Item 2 — session_id + multiple tasks
# --------------------------------------------------------------------------

def _ctx() -> ToolContext:
    ctx = ToolContext()
    ctx.config = types.SimpleNamespace(lock_subagent_model=False, prompt_roots=None)
    return ctx


def test_session_id_with_multiple_tasks_is_rejected():
    error, prepared = meta._prepare_fan_out(
        ["one", "two"], agent_id="worker", session_id="s1", model="", context=_ctx()
    )
    assert prepared is None
    assert error is not None
    assert error.startswith("ERROR: session_id runs one task per session")


def test_session_id_with_single_task_is_allowed():
    error, prepared = meta._prepare_fan_out(
        ["only"], agent_id="worker", session_id="s1", model="", context=_ctx()
    )
    assert error is None
    assert prepared is not None


def test_multiple_tasks_without_session_id_is_allowed():
    error, prepared = meta._prepare_fan_out(
        ["one", "two", "three"], agent_id="worker", session_id=None, model="", context=_ctx()
    )
    assert error is None
    assert prepared is not None


# --------------------------------------------------------------------------
# Item 3 — width cap on the fan-out
# --------------------------------------------------------------------------

def test_worker_cap_never_exceeds_configured_width(monkeypatch):
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

    monkeypatch.setattr(meta, "_run_one_task_async", probe)

    ctx = _ctx()
    ctx.subagent_max_workers = 3
    results = asyncio.run(
        meta.task_async(tasks=[f"t{n}" for n in range(10)], agent_id="worker", context=ctx)
    )

    assert peak <= 3
    assert "TASK_RESULTS agent=worker" in results
    assert results.count(". done") == 10


def test_worker_cap_defaults_below_the_dispatch_pool(monkeypatch):
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

    monkeypatch.setattr(meta, "_run_one_task_async", probe)

    ctx = _ctx()  # no override -> the js/jsrc SUBAGENT_WORKERS_MAX
    asyncio.run(meta.task_async(tasks=[f"t{n}" for n in range(20)], agent_id="worker", context=ctx))

    assert peak <= settings.default_value("SUBAGENT_WORKERS_MAX")


def _nested_probe(cap: int, depth_max: int, width: int, seen: list[int]):
    """A worker that fans out `width` children until `depth_max`, recording how
    many gate slots are taken while it runs."""

    async def probe(idx, total, item, context, parent_cfg, full_registry, agent_id, session_id, model=""):
        child = ToolContext()
        child.config = context.config
        child.subagent_max_workers = cap
        child.task_max_depth = depth_max
        child.task_depth = getattr(context, "task_depth", 0) + 1
        seen.append(meta._GATE.running)
        await asyncio.sleep(0.005)
        if child.task_depth < depth_max:
            await meta.task_async(tasks=[f"t{n}" for n in range(width)], agent_id="worker", context=child)
        seen.append(meta._GATE.running)
        return "done"

    return probe


def _tree_ctx(cap: int, depth_max: int) -> ToolContext:
    ctx = _ctx()
    ctx.subagent_max_workers = cap
    ctx.task_max_depth = depth_max
    return ctx


def test_worker_cap_holds_across_the_whole_tree(monkeypatch):
    seen: list[int] = []
    monkeypatch.setattr(meta, "_run_one_task_async", _nested_probe(4, 4, 4, seen))

    asyncio.run(meta.task_async(tasks=[f"t{n}" for n in range(4)], agent_id="worker", context=_tree_ctx(4, 4)))

    # 4 + 16 + 64 + 256 subagents ran, two samples each.
    assert len(seen) == 2 * (4 + 16 + 64 + 256)
    assert max(seen) <= 4
    assert meta._GATE.running == 0


def test_parent_waiting_on_children_lends_its_slot(monkeypatch):
    # One slot, three levels: a parent that kept its slot while waiting would
    # leave its child no slot to run in.
    seen: list[int] = []
    monkeypatch.setattr(meta, "_run_one_task_async", _nested_probe(1, 3, 2, seen))

    async def run():
        return await asyncio.wait_for(
            meta.task_async(tasks=["a", "b"], agent_id="worker", context=_tree_ctx(1, 3)), 10)

    assert asyncio.run(run()).count("done") == 2
    assert max(seen) <= 1
    assert meta._GATE.running == 0


def test_cancelled_waiter_gives_back_nothing_it_did_not_take():
    async def run():
        gate = meta.SubagentGate()
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
    gate = meta.SubagentGate()
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


def test_a_background_job_that_fails_hands_back_every_slot(monkeypatch):
    from js import snippets

    async def probe(idx, total, item, context, parent_cfg, full_registry, agent_id, session_id, model=""):
        if item == "boom":
            await asyncio.sleep(0.05)
            raise snippets.SnippetError("missing snippet")
        await asyncio.sleep(30)
        return "slow"

    monkeypatch.setattr(meta, "_run_one_task_async", probe)

    ctx = _tree_ctx(2, 3)
    started = meta.task(tasks=["boom", "slow", "slower"], agent_id="worker", background=True, context=ctx)
    handle = started.rsplit("HANDLE ", 1)[1].split()[0]
    job = meta.task_jobs.find(handle, ctx.task_owner)
    with pytest.raises(snippets.SnippetError):
        meta.task_jobs.wait(job, 5)
    deadline = time.monotonic() + 5
    while meta._GATE.running and time.monotonic() < deadline:
        time.sleep(0.02)

    assert meta._GATE.running == 0


def test_a_timed_wait_returns_on_time_while_its_job_is_queued(monkeypatch):
    waited: list[float] = []

    async def probe(idx, total, item, context, parent_cfg, full_registry, agent_id, session_id, model=""):
        if item == "child":
            await asyncio.sleep(0.5)
            return "child done"
        child = ToolContext()
        child.config = context.config
        child.subagent_max_workers = 1
        child.task_max_depth = 3
        child.task_depth = 1
        started = await meta.task_async(tasks=["child"], agent_id="worker", background=True, context=child)
        handle = started.rsplit("HANDLE ", 1)[1].split()[0]
        await asyncio.sleep(0.1)  # the child is queued behind this slot
        began = time.monotonic()
        await meta.task_async(action="wait", handle=handle, timeout=0, context=child)
        waited.append(time.monotonic() - began)
        await meta.task_async(action="wait", handle=handle, context=child)
        return "parent done"

    monkeypatch.setattr(meta, "_run_one_task_async", probe)

    result = asyncio.run(asyncio.wait_for(
        meta.task_async(tasks=["parent"], agent_id="worker", context=_tree_ctx(1, 3)), 10))

    assert result == "parent done"
    assert waited and waited[0] < 0.3
    assert meta._GATE.running == 0


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
    ctx = ToolContext()
    ctx.config = types.SimpleNamespace(lock_subagent_model=True, prompt_roots=None)

    error, prepared = meta._prepare_fan_out(
        ["do it"], agent_id="worker", session_id=None, model="", context=ctx
    )
    assert error is None and prepared is not None
    assert seen == [()]


def test_unlocked_subagent_model_keeps_override_flag(monkeypatch):
    seen = _capture_flags(monkeypatch)
    error, prepared = meta._prepare_fan_out(
        ["do it"], agent_id="worker", session_id=None, model="", context=_ctx()
    )
    assert error is None and prepared is not None
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
    context.subagent_assembly = meta.SubagentAssembly(
        config_factory=lambda **kwargs: cfg,
        registry_factory=lambda *args, **kwargs: registry,
        worker=worker,
    )
    error, prepared = meta._prepare_fan_out(["work"], "worker", None, "", context)
    assert error is None
    items, factory = prepared
    assert asyncio.run(factory(*items[0])) == "WORK"
    assert seen == [(context, cfg, registry)]
