"""Cluster coverage for the meta/registry fan-out fixes:

- session_id + multiple tasks is rejected cleanly (never silently shares a file).
- one fan-out never runs more workers than the width cap.
- the subagent-model lock strips the `model` override at every depth.
- a typo'd exact tool selector warns instead of silently shrinking the surface.
"""

from __future__ import annotations

import asyncio
import types

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

    ctx = _ctx()  # no override -> module default (8)
    asyncio.run(meta.task_async(tasks=[f"t{n}" for n in range(20)], agent_id="worker", context=ctx))

    assert peak <= meta._DEFAULT_SUBAGENT_MAX_WORKERS


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
    result = select(["reed"])
    assert [tool.name for tool in result.tools] == []
    err = capsys.readouterr().err
    assert "reed" in err and "matched no tool" in err


def test_exact_selector_miss_names_the_agent(capsys):
    full = build_default_registry()
    full.select(["fs_read"], agent_id="myagent")
    err = capsys.readouterr().err
    assert "'fs_read'" in err
    assert "myagent" in err


def test_glob_selector_miss_stays_silent(capsys):
    result = select(["zzz_*"])
    assert [tool.name for tool in result.tools] == []
    assert capsys.readouterr().err == ""


def test_metachar_selector_is_treated_as_glob(capsys):
    # `?` / `[` with no `*` must route through the (silent) glob branch, not the
    # exact-lookup warn branch.
    result = select(["read?"])
    assert [tool.name for tool in result.tools] == []
    assert capsys.readouterr().err == ""


def test_good_exact_and_glob_selectors_still_resolve(capsys):
    names = [tool.name for tool in select(["read", "todo_*"]).tools]
    assert "read" in names
    assert "todo_write" in names and "todo_read" in names
    assert capsys.readouterr().err == ""
