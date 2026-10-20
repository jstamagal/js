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


def test_codex_context_window_comes_from_the_model_catalog(monkeypatch):
    from js import runtime

    monkeypatch.setattr(runtime.model_metadata, "context_window", lambda *args: 1_050_000)

    assert runtime._resolve_context_window("gpt-5.6-sol", "openai-codex", None) == 1_050_000


def test_concurrent_turns_keep_their_own_overflow_window(tmp_path, monkeypatch):
    import asyncio

    import ai
    from ai.types.usage import Usage

    from js import model_client, runtime
    from js.model_client import ModelStreamResult
    from js.toolkit import ToolContext
    from js.toolkit.registry import ToolRegistry
    from js.turn_budget import TurnBudget
    from test_config_compaction_layers import _compact_test_cfg

    recovered = []

    async def recover(self, error, **kwargs):
        recovered.append(error.context_window)
        return False

    monkeypatch.setattr(TurnBudget, "recover_overflow", recover)
    monkeypatch.setattr(runtime.model_metadata, "resolve_max_output", lambda *args: 10)

    async def drive():
        waiting = asyncio.Event()
        other_finished = asyncio.Event()

        async def stream(**kwargs):
            small = kwargs["messages"][-1].parts[0].text == "small"
            if small:
                waiting.set()
                await other_finished.wait()
            return ModelStreamResult(
                text="done", tool_calls=[], reasoning="", finish_reason="stop",
                usage=Usage(input_tokens=1500 if small else 10, output_tokens=1),
                assistant_message=ai.assistant_message("done"),
            )

        monkeypatch.setattr(model_client, "stream_model_async", stream)

        async def turn(name, window):
            cfg = _compact_test_cfg(tmp_path / name, {
                "CONTEXT_SIZE_PER_MODEL": {"offline-test-model": window},
            })
            await runtime.run_turn_async(
                cfg, "SYSTEM", [{"role": "user", "content": name}], runtime.Telemetry(None),
                tool_registry=ToolRegistry((), {}), tool_context=ToolContext(cwd=tmp_path),
                suppress_output=True,
            )

        first = asyncio.create_task(turn("small", 1000))
        try:
            await waiting.wait()
            await turn("large", 10000)
        finally:
            other_finished.set()
            await first

    asyncio.run(drive())
    assert recovered == [1000]


def test_signed_reasoning_payload_counts_toward_the_estimate():
    plain = {"role": "assistant", "content": "ok", "reasoning_content": "thought"}
    signed = {**plain, "reasoning_parts": [
        {"text": "thought", "provider_metadata": {"openai": {"encrypted_content": "x" * 4000}}},
    ]}

    grown = context_budget.estimate_message_tokens(signed) - context_budget.estimate_message_tokens(plain)
    assert grown >= 1000


def test_non_ascii_word_characters_are_counted():
    """ALL-CLAIMS:169: CJK and accented letters matched no token branch."""
    japanese = "日本語のテキスト" * 100      # 800 chars
    assert context_budget.estimate_text_tokens(japanese) >= 400   # was 200 (chars/4 floor)
    assert context_budget.estimate_text_tokens("é" * 400) >= 200  # was 100
    assert context_budget.estimate_text_tokens("Привет мир " * 40) > 110  # was the floor, 110


def test_ascii_estimates_do_not_move():
    for text, expected in (("hello world " * 50, 150), ('{"a":1}' * 100, 700), ("x = foo(bar, baz)\n" * 30, 240)):
        assert context_budget.estimate_text_tokens(text) == expected, text
