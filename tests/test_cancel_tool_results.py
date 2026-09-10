"""Cancellation retains known outcomes without dispatching the rest of a turn."""
import asyncio
import threading
from dataclasses import replace

import pytest

from js import memory, runtime, supervisor
from js.toolkit.core import Tool, ToolContext
from js.toolkit.registry import ToolRegistry
from test_lazy_tool_discovery import _cfg, _result


@pytest.mark.parametrize("mode", ["sync", "async", "mixed_sync", "mixed_async"])
def test_cancel_keeps_completed_results_and_stops_forward_work(tmp_path, monkeypatch, mode):
    async_handler = mode in {"async", "mixed_async"}
    messages = [{"role": "user", "content": "run"}]
    ran = []
    entered = threading.Event()
    release = threading.Event()

    def sync_tool(n, context=None):
        ran.append(n)
        if n == 2:
            entered.set()
            assert release.wait(3)
        return f"result {n}"

    async def async_tool(n, context=None):
        ran.append(n)
        if n == 2:
            entered.set()
            await asyncio.Event().wait()
        return f"result {n}"

    tools = [Tool("probe", "test", async_tool if async_handler else sync_tool,
                  {"n": {"type": "integer"}})]
    third_tool = "probe"
    if mode.startswith("mixed"):
        third_tool = "other"
        tools.append(Tool("other", "test", sync_tool if async_handler else async_tool,
                          {"n": {"type": "integer"}}))
    registry = ToolRegistry(tuple(tools), {})
    monkeypatch.setattr(supervisor, "get_current", lambda: object())
    monkeypatch.setattr(runtime.model_client, "stream_model_async", lambda **kw: _result(
        ("c1", "probe", '{"n":1}'), ("c2", "probe", '{"n":2}'), ("c3", third_tool, '{"n":3}')))

    async def run():
        job = asyncio.create_task(runtime.run_turn_async(
            _cfg(tmp_path), "system", messages, runtime.Telemetry(None),
            tool_registry=registry, tool_context=ToolContext(cwd=tmp_path), suppress_output=True))
        while not entered.is_set():
            if job.done():
                await job
                pytest.fail(repr(messages))
            await asyncio.sleep(0.001)
        job.cancel()
        # Release from another thread: the old executor shutdown blocks the loop.
        timer = threading.Timer(0.05, release.set)
        timer.start()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(job, 2)
        timer.join()

    asyncio.run(run())
    assert ran == [1, 2]
    answered = {m["tool_call_id"]: m["content"] for m in messages if m["role"] == "tool"}
    assert answered == ({"c1": "result 1"} if async_handler else {"c1": "result 1", "c2": "result 2"})
    healed = memory.balance_orphaned_tool_calls(messages)
    assert healed[-1]["content"] == "ERROR: tool result was not recorded (session interrupted)"

    path = tmp_path / "saved.jsonl"
    for message in messages:
        memory.append_message(path, message)
    loaded = memory.load_messages(path)
    for call_id, result in answered.items():
        assert next(m["content"] for m in loaded if m.get("tool_call_id") == call_id) == result


def test_cancel_retains_completed_fan_out_and_sync_results_with_batch_cap(tmp_path, monkeypatch):
    from js.toolkit import meta

    messages = [{"role": "user", "content": "run"}]
    release = threading.Event()
    entered = threading.Event()

    def leaf(context=None):
        entered.set()
        assert release.wait(3)
        return "L" * 1000

    def fan(context=None):
        raise AssertionError("fan-out must stay on the event loop")

    async def dispatch_fan(tool, args, context):
        if args["n"] == 1:
            return "F" * 1000
        await asyncio.Event().wait()

    monkeypatch.setattr(meta, "is_fan_out_handler", lambda handler: handler is fan)
    monkeypatch.setattr(meta, "dispatch_fan_out_async", dispatch_fan)
    monkeypatch.setattr(supervisor, "get_current", lambda: object())
    registry = ToolRegistry((Tool("fan", "test", fan, {"n": {"type": "integer"}}),
                             Tool("leaf", "test", leaf, {})), {})
    monkeypatch.setattr(runtime.model_client, "stream_model_async", lambda **kw: _result(
        ("f1", "fan", '{"n":1}'), ("f2", "fan", '{"n":2}'), ("l1", "leaf", '{}')))
    cfg = replace(_cfg(tmp_path), max_tool_results_per_turn_bytes=300)

    async def run():
        job = asyncio.create_task(runtime.run_turn_async(
            cfg, "system", messages, runtime.Telemetry(None),
            tool_registry=registry, tool_context=ToolContext(cwd=tmp_path), suppress_output=True))
        while not entered.is_set():
            if job.done():
                await job
                pytest.fail(repr(messages))
            await asyncio.sleep(0.001)
        job.cancel()
        # Repeated Ctrl-C cannot throw away the running sync result either.
        await asyncio.sleep(0.01)
        job.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(job, 2)

    asyncio.run(run())
    answered = {m["tool_call_id"]: m["content"] for m in messages if m["role"] == "tool"}
    assert list(answered) == ["f1", "l1"]
    assert list(answered.values()) == runtime._cap_batch_results(["F" * 1000, "L" * 1000], 300)
    healed = memory.balance_orphaned_tool_calls(messages)
    assert next(m["content"] for m in healed if m.get("tool_call_id") == "f2") == (
        "ERROR: tool result was not recorded (session interrupted)"
    )
