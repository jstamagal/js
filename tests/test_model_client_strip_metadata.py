import copy

import ai

from js import model_client


def test_strip_reasoning_preserves_sdk_message_metadata_and_parts():
    reasoning = ai.types.messages.ReasoningPart(text="private reasoning")
    text = ai.types.messages.TextPart(text="answer", provider_metadata={"text": {"marker": 1}})
    tool = ai.types.messages.ToolCallPart(tool_call_id="call-1", tool_name="read", tool_args='{"file":"x"}')
    message = ai.messages.Message(
        role="assistant", parts=[reasoning, text, tool], id="original-id", turn_id="turn-1",
        usage=ai.types.usage.Usage(input_tokens=10, output_tokens=4), replay=True,
        provider_metadata={"provider": {"nested": [1, 2]}, "js": {"reasoning_blocks": [{"text": "kept metadata"}]}},
    )
    before = copy.deepcopy(message.model_dump())
    result = model_client._strip_reasoning_parts([message])
    assert len(result) == 1
    stripped = result[0]
    assert stripped is not message
    assert stripped.id == message.id
    assert stripped.turn_id == message.turn_id
    assert stripped.usage == message.usage
    assert stripped.replay is True
    assert stripped.provider_metadata == message.provider_metadata
    assert stripped.parts == [text, tool]
    assert stripped.parts[0] is text
    assert stripped.parts[1] is tool
    assert message.model_dump() == before
    assert message.parts[0] is reasoning


def test_strip_reasoning_only_messages_and_untouched_messages():
    only_reasoning = ai.assistant_message(ai.types.messages.ReasoningPart(text="reason"))
    assistant = ai.assistant_message("answer")
    user = ai.user_message("question")
    assert model_client._strip_reasoning_parts([user, only_reasoning, assistant]) == [user, assistant]
    result = model_client._strip_reasoning_parts([user, assistant])
    assert result[0] is user
    assert result[1] is assistant
