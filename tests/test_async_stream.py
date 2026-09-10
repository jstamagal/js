"""The async primitive shares one event loop: many turns/subagents overlap
instead of each spinning its own throwaway loop (the old asyncio.run-per-call)."""

from __future__ import annotations

import asyncio
import contextlib
import logging

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

    async def fake_stream(*, model, messages, tools, params, on_text):
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


@contextlib.asynccontextmanager
async def _teardown_that_awaits(inner):
    """httpcore2's `safe_async_iterate`: an @asynccontextmanager whose teardown
    awaits, so it cannot finish inside the GeneratorExit that closes it."""
    try:
        yield inner
    finally:
        await asyncio.sleep(0)
        await inner.aclose()


async def _transport_body_iterator():
    """Stands in for `PoolByteStream.__aiter__` — abandoned suspended at a yield
    once the SDK has parsed the last event it cared about."""

    async def chunks():
        yield b"first"
        yield b"second"

    async with _teardown_that_awaits(chunks()) as iterator:
        async for chunk in iterator:
            yield chunk


def test_sync_boundary_swallows_only_the_close_protocol_noise(caplog):
    """A clean `-p` run used to print a RuntimeError traceback after its own
    telemetry line, from the loop's shutdown_asyncgens pass. It is the reason
    openai is pinned to 2.x, so it has to stay dead."""
    abandoned: list[object] = []

    async def turn():
        body = _transport_body_iterator()
        abandoned.append(body)  # still suspended when the loop shuts down
        assert await anext(body) == b"first"
        return "finish=stop"

    with caplog.at_level(logging.ERROR, logger="asyncio"):
        assert model_client._run_owning_loop(turn()) == "finish=stop"

    assert [record.getMessage() for record in caplog.records] == []


def test_sync_boundary_still_reports_a_real_teardown_failure(caplog):
    """The filter must not become a blanket mute on asyncgen shutdown: a teardown
    that fails for a reason of its own still reaches the default handler."""
    abandoned: list[object] = []

    async def failing_body():
        try:
            yield b"first"
        finally:
            raise ValueError("boom")

    async def turn():
        body = failing_body()
        abandoned.append(body)
        assert await anext(body) == b"first"
        return "finish=stop"

    with caplog.at_level(logging.ERROR, logger="asyncio"):
        assert model_client._run_owning_loop(turn()) == "finish=stop"

    reported = "\n".join(record.getMessage() for record in caplog.records)
    assert "closing of asynchronous generator" in reported
    assert any(record.exc_info for record in caplog.records)
