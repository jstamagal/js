"""The async primitive shares one event loop: many turns/subagents overlap
instead of each spinning its own throwaway loop (the old asyncio.run-per-call)."""

from __future__ import annotations

import asyncio

import ai
import ai.types.usage

from js import model_client
from js.model_client import ModelStreamResult, stream_model_async


class _FakeProvider:
    async def aclose(self) -> None:
        pass


class _FakeModel:
    provider = _FakeProvider()


def _args(tag: str) -> dict:
    return dict(model_id=tag, provider_id=None, provider_base_url=None,
               provider_api_key=None, messages=[tag], tools=None,
               max_output_tokens=None, reasoning_effort=None, on_text=lambda _t: None)


def test_stream_model_async_runs_concurrently_on_one_loop(monkeypatch):
    order: list[tuple[str, str]] = []

    async def fake_stream(*, model, messages, tools, params, executor, on_text):
        tag = messages[0]
        order.append(("start", tag))
        await asyncio.sleep(0.05)
        order.append(("end", tag))
        return ModelStreamResult(
            text="ok", tool_calls=[], reasoning="",
            usage=ai.types.usage.Usage(input_tokens=0, output_tokens=2),
            finish_reason="stop", assistant_message=ai.assistant_message("ok"),
            first_token_s=0.01, elapsed_s=0.05,
        )

    monkeypatch.setattr(model_client, "resolve_model", lambda *a, **k: _FakeModel())
    monkeypatch.setattr(model_client, "_stream_async", fake_stream)

    async def drive():
        return await asyncio.gather(stream_model_async(**_args("A")),
                                    stream_model_async(**_args("B")))

    results = asyncio.run(drive())

    assert [r.text for r in results] == ["ok", "ok"]
    # Both STARTED before either ENDED → genuinely concurrent on the one loop.
    assert [o[0] for o in order] == ["start", "start", "end", "end"]
