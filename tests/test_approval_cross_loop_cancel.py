"""Approval cancellation across real event loops preserves caller control flow."""

import asyncio
import threading

import pytest

from js.events import Approval


@pytest.fixture
def foreign_loop():
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    loop.call_soon(ready.set)
    thread = threading.Thread(target=loop.run_forever)
    thread.start()
    assert ready.wait(2)
    try:
        yield loop
    finally:
        async def drain():
            pending = asyncio.all_tasks() - {asyncio.current_task()}
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        asyncio.run_coroutine_threadsafe(drain(), loop).result(timeout=2)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        assert not thread.is_alive()
        loop.close()


@pytest.mark.parametrize("repeat", range(3))
@pytest.mark.parametrize("mode", ["decision", "caller", "both"])
def test_cross_loop_cancellation_distinguishes_caller(foreign_loop, repeat, mode):
    entered = threading.Event()
    finished = threading.Event()

    async def decide(call):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            finished.set()

    approval = Approval(decide, foreign_loop)

    async def drive():
        task = asyncio.create_task(approval.refusal({"name": "shell"}))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            if mode in {"caller", "both"}:
                task.cancel()
            if mode in {"decision", "both"}:
                approval.cancel()
            if mode == "decision":
                result = await asyncio.wait_for(task, timeout=2)
                assert result.startswith("ERROR")
                assert task.cancelling() == 0
            else:
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=2)
                assert task.cancelling() > 0
            assert await asyncio.to_thread(finished.wait, 2)
            assert not approval._pending
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(drive())


@pytest.mark.parametrize("answer", [None, "refused", ValueError("bad decision")])
def test_cross_loop_ordinary_approval_outcomes_unchanged(foreign_loop, answer):
    async def decide(call):
        if isinstance(answer, Exception):
            raise answer
        return answer

    approval = Approval(decide, foreign_loop)
    result = asyncio.run(approval.refusal({"name": "shell"}))
    if isinstance(answer, Exception):
        assert result.startswith("ERROR")
    else:
        assert result == answer
    assert not approval._pending
