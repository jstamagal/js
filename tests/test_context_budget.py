from __future__ import annotations

from types import SimpleNamespace

from js import context_budget


def test_token_state_prefers_provider_usage_and_estimates_only_delta():
    state = context_budget.TokenState(chars_per_token=4.0)
    messages = [
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
    ]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "read",
                "description": "read a file",
                "parameters": {"type": "object"},
            },
        }
    ]
    state.record_provider_usage(
        SimpleNamespace(
            input_tokens=100,
            output_tokens=20,
            cache_read_tokens=7,
            cache_write_tokens=3,
        ),
        message_count=len(messages),
        system="system",
        tools=tools,
    )

    delta = {"role": "user", "content": "new " * 80}
    current, estimate, used_provider = state.current_context_tokens(
        system="system",
        messages=[*messages, delta],
        tools=tools,
    )

    assert used_provider is True
    assert current == 130 + context_budget.estimate_message_tokens(delta)
    assert estimate.total_tokens > context_budget.estimate_message_tokens(delta)


def test_tokens_until_compaction_uses_output_and_buffer_reserve():
    state = context_budget.TokenState(chars_per_token=4.0)
    messages = [{"role": "user", "content": "x" * 100}]

    status = state.budget_status(
        system="sys",
        messages=messages,
        tools=[],
        context_window=80,
        output_reserve_tokens=20,
        buffer_tokens=10,
    )

    assert status.effective_input_limit == 50
    assert status.tokens_until_compaction == 50 - status.current_context_tokens
    assert status.should_compact is (status.current_context_tokens > 50)
