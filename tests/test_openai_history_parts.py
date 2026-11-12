"""OpenAI text mappings translate at the provider history boundary."""

import ai
import pytest

from js import model_client


@pytest.mark.parametrize("role", ["user", "assistant"])
def test_openai_text_mappings_mix_with_strings_and_sdk_parts(role):
    sdk_text = ai.types.messages.TextPart(text="sdk")
    content = ["plain", {"type": "text", "text": "mapped"}, sdk_text]
    [message] = model_client.history_to_ai_messages("", [{"role": role, "content": content}])
    assert message.role == role
    assert [part.text for part in message.parts] == ["plain", "mapped", "sdk"]


@pytest.mark.parametrize("role", ["user", "assistant"])
@pytest.mark.parametrize("item", [
    {"type": "text"}, {"type": "text", "text": None}, {"type": "text", "text": 7},
    {"type": "text", "text": {"nested": "text"}},
    {"type": "image_url", "image_url": {"url": "https://example.test/image"}},
    {"text": "missing type"}, {"kind": "text", "text": "not a SDK part"},
])
def test_malformed_or_unsupported_part_mappings_still_fail(role, item):
    with pytest.raises(ValueError):
        model_client.history_to_ai_messages("", [{"role": role, "content": [item]}])


def test_sdk_parts_are_preserved_by_identity():
    text = ai.types.messages.TextPart(text="text")
    reasoning = ai.thinking("reasoning")
    parts = model_client._coerce_parts([text, reasoning])
    assert parts[0] is text
    assert parts[1] is reasoning


def test_empty_mapped_text_preserves_existing_empty_reply_behavior():
    content = [{"type": "text", "text": ""}]
    [user] = model_client.history_to_ai_messages("", [{"role": "user", "content": content}])
    assert user.parts[0].text == ""
    assert model_client.history_to_ai_messages("", [{"role": "assistant", "content": content}]) == []
