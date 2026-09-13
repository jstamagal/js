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
    assert current == 120 + context_budget.estimate_message_tokens(delta)
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


def test_calibration_uses_recorded_prompt_tokens():
    from types import SimpleNamespace
    from js.context_budget import TokenState

    state = TokenState()
    messages = [{"role": "user", "content": "words " * 100}]
    state.record_provider_usage(
        SimpleNamespace(input_tokens=50, cache_read_tokens=50, output_tokens=10),
        message_count=1, messages=messages,
    )
    assert state.last_usage.prompt_tokens == 50
    assert state.calibrated_chars_per_token(messages=messages) > 4.0


def test_cached_prompt_is_counted_once_against_128k_window():
    from ai.types.usage import Usage

    messages = [{"role": "user", "content": "active task"}]
    state = context_budget.TokenState()
    usage = Usage(input_tokens=64000, cache_read_tokens=63000, output_tokens=500)
    state.record_provider_usage(usage, message_count=1, messages=messages)
    budget = state.budget_status(messages=messages, context_window=128000, buffer_tokens=4096)
    assert state.last_usage.total_tokens == usage.total_tokens == 64500
    assert budget.current_context_tokens == 64500
    assert budget.should_compact is False
