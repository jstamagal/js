import asyncio

import ai
import pytest

from js import codex_provider
from test_codex_reasoning_replay import _Client, _fake_jwt, _stream


def _summary_events(items, *, delta_indexes=True):
    events = []
    for number, parts in enumerate(items):
        item_id = f"rs_{number}"
        for index, chunks in enumerate(parts):
            common = {"item_id": item_id, "output_index": number, "summary_index": index}
            events.append({"type": "response.reasoning_summary_part.added", **common,
                           "part": {"type": "summary_text", "text": ""}})
            for chunk in chunks:
                fields = common if delta_indexes else {key: value for key, value in common.items() if key != "summary_index"}
                events.append({"type": "response.reasoning_summary_text.delta", **fields, "delta": chunk})
            events.append({"type": "response.reasoning_summary_part.done", **common,
                           "part": {"type": "summary_text", "text": "".join(chunks)}})
        events.append({"type": "response.output_item.done", "item": {
            "id": item_id, "type": "reasoning", "summary": [
                {"type": "summary_text", "text": "".join(chunks)} for chunks in parts],
        }})
    events += [{"type": "response.output_text.delta", "delta": "answer"},
               {"type": "response.completed", "response": {}}]
    return events


@pytest.mark.parametrize(("parts", "expected"), [
    ([["one."], ["**Next heading**"]], "one.\n\n**Next heading**"),
    ([["a", "b"], ["c", "d"]], "ab\n\ncd"),
    ([[], ["only"], []], "only"),
    ([["single"]], "single"),
    ([["first\n"], ["\nsecond"]], "first\n\nsecond"),
    ([["first\n\n"], ["second"]], "first\n\nsecond"),
])
@pytest.mark.parametrize("delta_indexes", [True, False])
def test_summary_parts_have_streamed_and_assembled_boundaries(monkeypatch, parts, expected, delta_indexes):
    events = _summary_events([parts], delta_indexes=delta_indexes)
    result, _body = _stream(monkeypatch, events)
    assert result.assistant_message.reasoning == expected

    async def collect():
        provider = codex_provider.OpenAICodexProvider(
            access_token=_fake_jwt(), account_id="acct_1", client=_Client([events]),
        )
        return [event async for event in provider.stream(
            ai.Model(id="gpt-5.5", provider=provider), [ai.user_message("hello")], tools=None, params=None,
        )]

    streamed = asyncio.run(collect())
    assert "".join(event.chunk for event in streamed if isinstance(event, ai.events.ReasoningDelta)) == expected


def test_separate_reasoning_items_keep_independent_parts(monkeypatch):
    result, _body = _stream(monkeypatch, _summary_events([[["one"], ["two"]], [["three"]]]))
    assert [part.text for part in result.assistant_message.parts if isinstance(part, ai.types.messages.ReasoningPart)] == [
        "one\n\ntwo", "three",
    ]
