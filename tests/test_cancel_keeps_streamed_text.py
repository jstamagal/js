"""Text already streamed to the screen must land in history when the turn is
cancelled mid-stream. Otherwise the terminal shows an answer the model cannot
see, and the REPL's "produced nothing" branch drops the user's prompt with it."""

from __future__ import annotations

import asyncio

import pytest

from js import runtime

from test_runtime_cluster_fixes import _Recorder, _cfg


def _cancel_midstream(tmp_path, monkeypatch, stream):
    messages = [{"role": "user", "content": "name 20 gorilla facts"}]
    monkeypatch.setattr(runtime.model_client, "stream_model_async", stream)

    async def drive():
        task = asyncio.get_running_loop().create_task(
            runtime.run_turn_async(
                _cfg(tmp_path), "SYS", messages,
                runtime.Telemetry(debug_log=None),
                suppress_output=True, event_hooks=_Recorder(),
            )
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(drive())
    return messages


def test_cancel_midstream_keeps_streamed_text(tmp_path, monkeypatch):
    async def slow_stream(**kwargs):
        on_text = kwargs.get("on_text")
        if on_text:
            on_text("1. gorillas catch human colds")
        await asyncio.sleep(30)

    messages = _cancel_midstream(tmp_path, monkeypatch, slow_stream)

    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[1]["content"] == "1. gorillas catch human colds"
    assert messages[1]["incomplete_reason"] == "cancelled"


def test_cancel_before_any_text_appends_nothing(tmp_path, monkeypatch):
    async def silent_stream(**kwargs):
        await asyncio.sleep(30)

    messages = _cancel_midstream(tmp_path, monkeypatch, silent_stream)

    assert [m["role"] for m in messages] == ["user"]
