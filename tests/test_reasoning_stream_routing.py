"""Reasoning chunks reach the callback of the stream that produced them."""

from __future__ import annotations

import asyncio

import ai
import ai.types.events as events

from js import model_client, settings


def _scripted(monkeypatch):
    def stream(**kwargs):
        tag = kwargs["messages"][-1].text

        async def generate():
            yield events.StreamStart()
            yield events.ReasoningStart(block_id="r")
            for n in range(3):
                yield events.ReasoningDelta(chunk=f"{tag}{n}", block_id="r")
                await asyncio.sleep(0)
            yield events.ReasoningEnd(block_id="r")
            yield events.TextStart(block_id="t")
            yield events.TextDelta(chunk=tag, block_id="t")
            yield events.TextEnd(block_id="t")
            yield events.StreamEnd()

        return ai.models.Stream(generate())

    monkeypatch.setattr(ai, "stream", stream)


def _call(tag: str, on_reasoning):
    return model_client.stream_model_async(
        model_id="qwen-test", provider_id="openai", provider_base_url="http://local.test/v1",
        provider_api_key="fixture", messages=[ai.user_message(tag)], tools=None,
        max_output_tokens=None, reasoning_effort=None, on_text=lambda _t: None,
        on_reasoning=on_reasoning,
    )


def test_concurrent_streams_deliver_reasoning_to_their_own_callbacks(monkeypatch):
    _scripted(monkeypatch)
    seen: dict[str, list[str]] = {"A": [], "B": []}

    async def drive():
        return await asyncio.gather(_call("A", seen["A"].append), _call("B", seen["B"].append))

    results = asyncio.run(drive())

    assert [r.text for r in results] == ["A", "B"]
    assert seen == {"A": ["A0", "A1", "A2"], "B": ["B0", "B1", "B2"]}


def test_stream_without_reasoning_callback_does_not_reach_an_earlier_one(monkeypatch):
    _scripted(monkeypatch)
    seen: list[str] = []

    async def drive():
        await _call("A", seen.append)
        await _call("B", None)

    asyncio.run(drive())

    assert seen == ["A0", "A1", "A2"]


def test_ui_reasoning_reads_its_canonical_env_var():
    overlaid = settings.apply_env_overrides(settings.seed_defaults(), {"JS_UI_REASONING": "1"})
    assert settings.get_dotted(overlaid, ("ui", "reasoning")) == 1

    ignored = settings.apply_env_overrides(settings.seed_defaults(), {"JS_UI_REASONING": "9"})
    assert settings.get_dotted(ignored, ("ui", "reasoning")) == settings.SPEC_BY_KEY["ui.reasoning"].default
