import asyncio
import types

import pytest

from js import supervisor
from js.toolkit import ToolContext, subagents


def _context(worker) -> ToolContext:
    """A context whose task tool runs ``worker(index, item)`` as each subagent."""
    async def subagent(idx, total, item, *args, **kwargs):
        return await worker(idx, item)

    context = ToolContext()
    context.config = types.SimpleNamespace(lock_subagent_model=False, prompt_roots=None)
    context.subagent_assembly = subagents.SubagentAssembly(worker=subagent)
    return context


@pytest.mark.parametrize("items", [["cancel"], ["ok", "cancel", "failure"]])
def test_sync_fanout_cancelled_results_match_async(items, monkeypatch):
    monkeypatch.setattr(supervisor, "get_current", lambda: None)

    async def worker(index, item):
        if item == "cancel":
            raise asyncio.CancelledError()
        if item == "failure":
            raise ValueError("worker failed")
        return f"completed {index}"

    assembled = subagents.task(tasks=items, agent_id="test-agent", context=_context(worker))
    expected = asyncio.run(subagents.task_async(tasks=items, agent_id="test-agent", context=_context(worker)))
    assert isinstance(assembled, str)
    assert assembled == expected
    assert "CancelledError" in assembled
    if len(items) > 1:
        assert "\n\n1. completed 1\n\n" in assembled
        assert "\n\n2. ERROR CancelledError" in assembled
        assert "\n\n3. ERROR ValueError" in assembled


@pytest.mark.parametrize("exception", [KeyboardInterrupt, SystemExit])
def test_sync_fanout_control_flow_propagates(exception, monkeypatch):
    monkeypatch.setattr(supervisor, "get_current", lambda: None)

    async def worker(index, item):
        raise exception()

    with pytest.raises(exception):
        subagents.task(tasks=["control flow"], agent_id="test-agent", context=_context(worker))
