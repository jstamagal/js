import asyncio

import pytest

from js import supervisor
from js.toolkit import meta


@pytest.mark.parametrize("items", [[(1, "cancel")], [(1, "ok"), (2, "cancel"), (3, "failure")]])
def test_sync_fanout_cancelled_results_match_async(items, monkeypatch):
    monkeypatch.setattr(supervisor, "get_current", lambda: None)

    async def worker(index, item):
        if item == "cancel":
            raise asyncio.CancelledError()
        if item == "failure":
            raise ValueError("worker failed")
        return f"completed {index}"

    results = meta._fan_out(items, worker)
    expected = asyncio.run(meta._fan_out_async(items, worker))
    assert all(isinstance(result, str) for result in results)
    assert results == expected
    assert "CancelledError" in results[[item for _, item in items].index("cancel")]
    assembled = meta._assemble_task_results(results, "test-agent", None)
    assert isinstance(assembled, str)
    assert "CancelledError" in assembled
    if len(items) > 1:
        assert results[0] == "completed 1"
        assert "ValueError" in results[2]


@pytest.mark.parametrize("exception", [KeyboardInterrupt, SystemExit])
def test_sync_fanout_control_flow_propagates(exception, monkeypatch):
    monkeypatch.setattr(supervisor, "get_current", lambda: None)

    async def worker(index, item):
        raise exception()

    with pytest.raises(exception):
        meta._fan_out([(1, "control flow")], worker)
