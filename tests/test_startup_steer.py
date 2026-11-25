"""A line typed while the agent's startup exchanges run is not lost."""

from __future__ import annotations

import asyncio

import ai
import ai.types.usage
from repl_driver import run_async, within
from test_agent_messages import _agent

from js import runtime
from js.config import from_env
from js.model_client import ModelStreamResult


def test_a_line_typed_during_startup_exchanges_is_sent_when_they_end(monkeypatch, tmp_path):
    _agent(tmp_path, monkeypatch, {"01-user.md": "opening"})
    monkeypatch.setenv("JS_STEER_MODE", "now")
    started, release = asyncio.Event(), asyncio.Event()
    asked: list[str] = []

    async def stream(**kwargs):
        last = kwargs["messages"][-1]
        asked.append("".join(part.text for part in last.parts if part.kind == "text"))
        if len(asked) == 1:
            started.set()
            await release.wait()
        return ModelStreamResult(
            text="reply", tool_calls=[], reasoning="",
            usage=ai.types.usage.Usage(input_tokens=0, output_tokens=1),
            finish_reason="stop", assistant_message=ai.assistant_message("reply"),
        )

    monkeypatch.setattr(runtime.model_client, "stream_model_async", stream)

    async def typing(enter):
        await within(started.wait(), "the startup exchange's model call")
        await enter("typed during startup")
        release.set()

    run_async(monkeypatch, from_env(session="seeded"), typing)

    assert asked == ["opening", "typed during startup"]
