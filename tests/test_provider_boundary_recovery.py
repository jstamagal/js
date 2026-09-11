import asyncio
import json
from dataclasses import replace

import ai
import pytest

from js import compaction, model_client, runtime
from js.toolkit import ToolContext
from js.toolkit.registry import build_default_registry
from test_lazy_tool_discovery import _cfg, _result


@pytest.fixture(autouse=True)
def offline_metadata(monkeypatch):
    monkeypatch.setattr(runtime, "_resolve_context_window", lambda *a, **k: 1_000_000)
    monkeypatch.setattr(runtime.model_metadata, "resolve_max_output", lambda *a, **k: 4096)


def _config(tmp_path):
    return replace(_cfg(tmp_path), provider_id="openai", provider_api_key="offline",
                   provider_base_url="http://127.0.0.1:1/v1")


def _overflow():
    return ai.ProviderAPIError(
        "request (97720 tokens) exceeds the available context size (89600 tokens)",
        provider="openai", error_type="exceed_context_size_error",
    )


@pytest.mark.parametrize("persistent", [False, True])
def test_summary_peels_through_real_boundary(monkeypatch, tmp_path, persistent):
    payloads = []
    error = _overflow()

    async def sdk(**kwargs):
        text = kwargs["messages"][0].parts[0].text
        payloads.append(json.loads(text.split("Session messages JSON:\n", 1)[1]))
        if persistent or len(payloads) == 1:
            raise error
        return _result(text="recovered")

    monkeypatch.setattr(model_client, "_stream_async", sdk)
    messages = [{"role": "user", "content": f"entry {i}"} for i in range(32)]
    call = compaction.summarize(_config(tmp_path), "offline-test", messages, "", "")
    if persistent:
        with pytest.raises(ai.ProviderAPIError) as caught:
            asyncio.run(call)
        assert caught.value is error
        assert [len(p) for p in payloads] == [32, 16, 8, 4]
    else:
        assert asyncio.run(call) == "recovered"
        assert payloads == [messages, messages[16:]]


@pytest.mark.parametrize("kind", ["overflow", "429", "503", "fatal"])
@pytest.mark.parametrize("persistent", [False, True])
def test_runtime_recovers_through_real_boundary(monkeypatch, tmp_path, kind, persistent):
    error = _overflow() if kind == "overflow" else ai.ProviderStatusError(
        f"HTTP {kind}", provider="openai", code=kind, is_retryable=kind != "fatal",
    )
    attempts = []

    async def sdk(**kwargs):
        attempts.append(kwargs)
        if persistent or len(attempts) == 1:
            raise error
        return _result(text="recovered")

    monkeypatch.setattr(model_client, "_stream_async", sdk)
    monkeypatch.setattr(runtime, "_backoff", lambda _: 0)
    messages = []
    for i in range(81):
        messages.extend([
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": f"c{i}", "type": "function", "function": {"name": "read", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": f"c{i}", "name": "read", "content": "x" * 1000},
        ])
    messages.append({"role": "user", "content": "continue"})
    call = runtime.run_turn_async(
        _config(tmp_path), "system", messages, runtime.Telemetry(None),
        tool_registry=build_default_registry().select([]),
        tool_context=ToolContext(cwd=tmp_path), suppress_output=True,
    )
    if persistent and kind == "overflow":
        asyncio.run(call)
        assert len(attempts) == 3
        assert messages[-1] == {"role": "user", "content": "continue"}
    elif persistent or kind == "fatal":
        with pytest.raises(ai.ProviderAPIError) as caught:
            asyncio.run(call)
        assert caught.value is error
    else:
        asyncio.run(call)
        assert messages[-1]["content"] == "recovered"
    if kind != "overflow":
        assert len(attempts) == (1 if kind == "fatal" else 3 if persistent else 2)
        assert all(m["content"] == "x" * 1000 for m in messages if m["role"] == "tool")
    else:
        assert len(attempts) <= compaction.MAX_OVERFLOW_ROUNDS + 1
        assert any(m["content"] == compaction.MICROCOMPACT_CLEARED_MESSAGE for m in messages)


def test_summary_does_not_peel_non_overflow(monkeypatch, tmp_path):
    error = ai.ProviderAPIError("invalid request", provider="openai")
    attempts = []

    async def sdk(**kwargs):
        attempts.append(kwargs)
        raise error

    monkeypatch.setattr(model_client, "_stream_async", sdk)
    with pytest.raises(ai.ProviderAPIError) as caught:
        asyncio.run(compaction.summarize(_config(tmp_path), "offline-test",
                                        [{"role": "user", "content": "x"}] * 12, "", ""))
    assert caught.value is error
    assert len(attempts) == 1