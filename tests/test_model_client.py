"""Tests for the ai-python SDK boundary in js/model_client.py.

No network calls. All model interactions use a fake ``StreamExecutor``.
"""

from __future__ import annotations

import ai
import ai.types.events
import ai.types.messages
import ai.types.tools
import ai.types.usage
import ai.models
import pytest

from js import model_client
from js.sampling import Sampling


class _FakeExecutor:
    """Yields scripted events for a single stream."""

    def __init__(self, events):
        self.events = events
        self.request = None

    async def _do_stream(self, request: ai.models.StreamRequest):
        self.request = request
        for event in self.events:
            yield event


def _text_events(text: str) -> list[ai.types.events.Event]:
    return [
        ai.types.events.StreamStart(),
        ai.types.events.TextStart(block_id="text"),
        ai.types.events.TextDelta(chunk=text, block_id="text"),
        ai.types.events.TextEnd(block_id="text"),
        ai.types.events.StreamEnd(),
    ]


def _tool_events(tool_call_id: str, name: str, args: str) -> list[ai.types.events.Event]:
    return [
        ai.types.events.StreamStart(),
        ai.types.events.ToolStart(tool_call_id=tool_call_id, tool_name=name),
        ai.types.events.ToolDelta(chunk=args, tool_call_id=tool_call_id),
        ai.types.events.ToolEnd(
            tool_call_id=tool_call_id,
            tool_call=ai.types.messages.DUMMY_TOOL_CALL,
        ),
        ai.types.events.StreamEnd(),
    ]


def _reasoning_events(text: str) -> list[ai.types.events.Event]:
    return [
        ai.types.events.StreamStart(),
        ai.types.events.ReasoningStart(block_id="reasoning"),
        ai.types.events.ReasoningDelta(chunk=text, block_id="reasoning"),
        ai.types.events.ReasoningEnd(block_id="reasoning"),
        ai.types.events.StreamEnd(),
    ]


def _usage_events(
    input_tokens: int = 100,
    output_tokens: int = 10,
    cache_read_tokens: int = 25,
) -> list[ai.types.events.Event]:
    return [
        ai.types.events.StreamStart(),
        ai.types.events.TextStart(block_id="text"),
        ai.types.events.TextDelta(chunk="done", block_id="text"),
        ai.types.events.TextEnd(block_id="text"),
        ai.types.events.StreamEnd(
            usage=ai.types.usage.Usage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens,
            )
        ),
    ]


def _pview(p) -> dict:
    """Flatten an ai>=0.2.1 InferenceRequestParams back to a plain dict so the
    param-shaping tests stay readable. Only set fields appear."""
    from ai.models.core import params as ap

    out: dict = {}
    if p is None:
        return out
    if p.output is not None and p.output.max_tokens is not None:
        out["max_tokens"] = p.output.max_tokens
    if isinstance(p.reasoning, ap.ReasoningParams):
        out["reasoning_effort"] = p.reasoning.effort
    samp = p.sampling
    if not isinstance(samp, ap.ModelProviderDefault):
        for cls, key in (
            (ap.TemperatureSamplerParams, "temperature"),
            (ap.TopPSamplerParams, "top_p"),
            (ap.TopKSamplerParams, "top_k"),
        ):
            if cls in samp:
                out[key] = getattr(samp[cls], key)
        rep = samp.get(ap.RepetitionPenaltyParams)
        if rep is not None:
            if not isinstance(rep.repetition_penalty, ap.ModelProviderDefault) and rep.repetition_penalty is not None:
                out["repetition_penalty"] = rep.repetition_penalty
            if not isinstance(rep.presence_penalty, ap.ModelProviderDefault) and rep.presence_penalty is not None:
                out["presence_penalty"] = rep.presence_penalty
    if p.extra_body:
        out["extra_body"] = dict(p.extra_body)
    return out


def _model_for_error_tests() -> ai.Model:
    return ai.Model(id="test", provider=ai.get_provider("openai", api_key="x"))


def _assert_one_line_friendly(message: str, *, provider: str, command: str) -> None:
    assert "\n" not in message
    assert provider in message
    assert command in message
    assert "Traceback" not in message


def test_resolve_model_gateway_needs_explicit_base_else_errors(monkeypatch):
    from js.routing import ProviderNotLoggedInError

    captured = {}

    def fake_get_model(model_id: str) -> ai.Model:
        captured["model_id"] = model_id
        return ai.Model(id="fake-id", provider=ai.get_provider("openai", api_key="x"))

    monkeypatch.setattr(ai, "get_model", fake_get_model)

    # No provider AND no base URL: refuse to ride the SDK's own env keys.
    with pytest.raises(ProviderNotLoggedInError):
        model_client.resolve_model(
            "deepseek/deepseek-v4-flash",
            provider_id=None,
            provider_base_url=None,
            provider_api_key=None,
        )
    assert "model_id" not in captured

    # An explicit base URL is a deliberate endpoint choice: the gateway path stands.
    result = model_client.resolve_model(
        "deepseek/deepseek-v4-flash",
        provider_id=None,
        provider_base_url="http://gateway.test/v1",
        provider_api_key=None,
    )
    assert captured["model_id"] == "deepseek/deepseek-v4-flash"
    assert result.id == "fake-id"


def test_stream_model_maps_missing_api_key_typeerror(monkeypatch):
    async def raise_missing_key(**_kwargs):
        raise TypeError(
            "Could not resolve authentication method. Expected either api_key or "
            "admin_api_key to be set."
        )

    monkeypatch.setattr(model_client, "resolve_model", lambda *_args, **_kwargs: _model_for_error_tests())
    monkeypatch.setattr(model_client, "_stream_async", raise_missing_key)

    with pytest.raises(model_client.FriendlyProviderError) as excinfo:
        model_client.stream_model(
            model_id="test",
            provider_id="openai",
            provider_base_url=None,
            provider_api_key=None,
            messages=[ai.user_message("hi")],
            tools=None,
            max_output_tokens=64,
            reasoning_effort=None,
            on_text=lambda _s: None,
        )

    message = str(excinfo.value)
    _assert_one_line_friendly(message, provider="openai", command="js --login openai")
    assert "set provider.api_key <value>" in message
    assert "TypeError" not in message
    assert "Could not resolve authentication method" not in message
    assert "Expected either api_key or admin_api_key" not in message


def test_stream_model_maps_unknown_sdk_provider_id(monkeypatch):
    def raise_unknown_provider(*_args, **_kwargs):
        raise ValueError("unknown provider id: 'stale'")

    monkeypatch.setattr(model_client, "resolve_model", raise_unknown_provider)

    with pytest.raises(model_client.FriendlyProviderError) as excinfo:
        model_client.stream_model(
            model_id="test",
            provider_id="stale",
            provider_base_url=None,
            provider_api_key=None,
            messages=[ai.user_message("hi")],
            tools=None,
            max_output_tokens=64,
            reasoning_effort=None,
            on_text=lambda _s: None,
        )

    message = str(excinfo.value)
    _assert_one_line_friendly(message, provider="stale", command="js --login stale")
    assert "js --list-models shows what's runnable" in message
    assert "ValueError" not in message
    assert "unknown provider id" not in message


def test_stream_model_maps_provider_authentication_error(monkeypatch):
    async def raise_auth(**_kwargs):
        raise ai.ProviderAuthenticationError(
            "Error code: 401 - Incorrect API key",
            provider="openai",
            http_context=ai.HTTPErrorContext(status_code=401),
        )

    monkeypatch.setattr(model_client, "resolve_model", lambda *_args, **_kwargs: _model_for_error_tests())
    monkeypatch.setattr(model_client, "_stream_async", raise_auth)

    with pytest.raises(model_client.FriendlyProviderError) as excinfo:
        model_client.stream_model(
            model_id="test",
            provider_id="openai",
            provider_base_url=None,
            provider_api_key="bad",
            messages=[ai.user_message("hi")],
            tools=None,
            max_output_tokens=64,
            reasoning_effort=None,
            on_text=lambda _s: None,
        )

    message = str(excinfo.value)
    _assert_one_line_friendly(message, provider="openai", command="js --login openai")
    assert "set provider.api_key <value>" in message
    assert "Incorrect API key" in message
    assert "ProviderAuthenticationError" not in message


def test_resolve_model_uses_explicit_provider_verbatim():
    result = model_client.resolve_model(
        "gemma4:e4b",
        provider_id="openai",
        provider_base_url="http://127.0.0.1:11434/v1",
        provider_api_key="ollama",
    )
    assert result.id == "gemma4:e4b"
    assert result.provider.name == "openai"
    assert result.provider.base_url == "http://127.0.0.1:11434/v1"


def test_resolve_model_allows_model_outside_static_allowlist():
    # allowed_models is a curated hint for filtering /models noise, not a gate.
    # A model absent from the static tuple (the endpoint may serve ids the list
    # has not caught up to, e.g. a new glm-5.2) must still resolve — the provider
    # is the authority and will reject genuinely-unknown ids itself.
    result = model_client.resolve_model(
        "glm-5.2",
        provider_id="opencode-go",
        provider_base_url="https://opencode.ai/zen/go/v1",
        provider_api_key="sk-test",
    )
    assert result.id == "glm-5.2"


def test_tool_specs_to_ai_tools_requires_function_type():
    with pytest.raises(ValueError, match="unsupported tool spec type"):
        model_client.tool_specs_to_ai_tools([{"type": "provider"}])


def test_tool_specs_to_ai_tools_requires_name():
    with pytest.raises(ValueError, match="missing function.name"):
        model_client.tool_specs_to_ai_tools([{"type": "function", "function": {}}])


def test_tool_specs_to_ai_tools_converts_spec():
    specs = [
        {
            "type": "function",
            "function": {
                "name": "fs_search",
                "description": "Search files",
                "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
            },
        }
    ]
    tools = model_client.tool_specs_to_ai_tools(specs)
    assert len(tools) == 1
    tool = tools[0]
    assert isinstance(tool, ai.types.tools.Tool)
    assert tool.name == "fs_search"
    assert isinstance(tool.spec, ai.types.tools.ToolSpec)
    assert tool.spec.description == "Search files"
    assert tool.spec.params["type"] == "object"


def test_history_to_ai_messages_preserves_roles():
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello", "reasoning_content": "stripped"},
        {
            "role": "assistant",
            "content": "call",
            "reasoning_content": "needed",
            "tool_calls": [
                {
                    "id": "tc1",
                    "type": "function",
                    "function": {"name": "fs_search", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "tc1", "name": "fs_search", "content": "42"},
    ]
    out = model_client.history_to_ai_messages("sys", msgs)
    assert [m.role for m in out] == [
        "system",
        "user",
        "assistant",
        "assistant",
        "tool",
    ]
    # Tool-free assistant reasoning is archive-only and should not be replayed.
    assert [p.kind for p in out[2].parts] == ["text"]
    # Assistant reasoning is replayed when the same assistant turn carried tool calls.
    tc_parts = out[3].parts
    assert [p.kind for p in tc_parts] == ["reasoning", "text", "tool_call"]
    assert tc_parts[2].tool_name == "fs_search"


def test_stream_model_collects_text_reasoning_tool_calls_and_usage():
    events = [
        ai.types.events.StreamStart(),
        ai.types.events.TextStart(block_id="text"),
        ai.types.events.TextDelta(chunk="DONE", block_id="text"),
        ai.types.events.TextEnd(block_id="text"),
        ai.types.events.ReasoningStart(block_id="reasoning"),
        ai.types.events.ReasoningDelta(chunk="thinking", block_id="reasoning"),
        ai.types.events.ReasoningEnd(block_id="reasoning"),
        ai.types.events.ToolStart(tool_call_id="tc1", tool_name="fs_search"),
        ai.types.events.ToolDelta(chunk='{"q":"x"}', tool_call_id="tc1"),
        ai.types.events.ToolEnd(
            tool_call_id="tc1",
            tool_call=ai.types.messages.DUMMY_TOOL_CALL,
        ),
        ai.types.events.StreamEnd(
            usage=ai.types.usage.Usage(
                input_tokens=100,
                output_tokens=10,
                cache_read_tokens=25,
            )
        ),
    ]
    executor = _FakeExecutor(events)
    emitted_text: list[str] = []
    result = model_client.stream_model(
        model_id="test",
        provider_id="openai",
        provider_base_url="http://localhost:11434/v1",
        provider_api_key="x",
        messages=[ai.user_message("hi")],
        tools=None,
        max_output_tokens=100,
        reasoning_effort=None,
        on_text=emitted_text.append,
        executor=executor,
    )
    assert result.text == "DONE"
    assert result.reasoning == "thinking"
    assert result.finish_reason == "tool_calls"
    assert len(result.tool_calls) == 1
    tc = result.tool_calls[0]
    assert tc.id == "tc1"
    assert result.usage.output_tokens == 10
    assert emitted_text == ["DONE"]
    assert executor.request is not None
    assert _pview(executor.request.params) == {"max_tokens": 100}


def test_stream_model_passes_reasoning_effort():
    executor = _FakeExecutor(_text_events("ok"))
    model_client.stream_model(
        model_id="test",
        provider_id="openai",
        provider_base_url="http://localhost:11434/v1",
        provider_api_key="x",
        messages=[ai.user_message("hi")],
        tools=None,
        max_output_tokens=64,
        reasoning_effort="low",
        on_text=lambda _s: None,
        executor=executor,
    )
    assert _pview(executor.request.params) == {"max_tokens": 64, "reasoning_effort": "low"}


def test_stream_model_passes_reasoning_none():
    executor = _FakeExecutor(_text_events("ok"))
    model_client.stream_model(
        model_id="test",
        provider_id="openai",
        provider_base_url="http://localhost:11434/v1",
        provider_api_key="x",
        messages=[ai.user_message("hi")],
        tools=None,
        max_output_tokens=64,
        reasoning_effort="none",
        on_text=lambda _s: None,
        executor=executor,
    )
    assert _pview(executor.request.params) == {"max_tokens": 64}


def test_stream_model_puts_deepseek_reasoning_budget_in_extra_body():
    executor = _FakeExecutor(_text_events("ok"))
    model_client.stream_model(
        model_id="deepseek-chat",
        provider_id="deepseek",
        provider_base_url=None,
        provider_api_key="x",
        messages=[ai.user_message("hi")],
        tools=None,
        max_output_tokens=64,
        reasoning_effort=None,
        on_text=lambda _s: None,
        executor=executor,
    )
    assert _pview(executor.request.params) == {"max_tokens": 64, "extra_body": {"max_reasoning_tokens": 32_000}}


def test_stream_model_strips_minimax_reasoning_by_model_prefix():
    executor = _FakeExecutor(_text_events("ok"))
    model_client.stream_model(
        model_id="minimax:text-01",
        provider_id=None,
        # Explicit base URL is the deliberate-endpoint escape hatch: with no
        # provider AND no base URL the model boundary now refuses to ride the SDK's
        # own env keys.
        provider_base_url="http://gateway.test/v1",
        provider_api_key=None,
        messages=[ai.user_message("hi")],
        tools=None,
        max_output_tokens=64,
        reasoning_effort="high",
        on_text=lambda _s: None,
        executor=executor,
    )
    assert _pview(executor.request.params) == {"max_tokens": 64}



def test_stream_model_applies_sampling_from_env_for_openai():
    """JS_* env sampling is typed before stream_model; OpenAI only receives supported knobs."""
    sampling = Sampling.from_env(
        {
            "JS_TEMP": "1.1",
            "JS_TOPP": "0.96",
            "JS_TOPK": "64",
            "JS_REPPEN": "1.05",
            "JS_PRPEN": "1.5",
        }
    )
    executor = _FakeExecutor(_text_events("ok"))
    model_client.stream_model(
        model_id="test",
        provider_id="openai",
        provider_base_url="http://localhost:11434/v1",
        provider_api_key="x",
        messages=[ai.user_message("hi")],
        tools=None,
        max_output_tokens=64,
        reasoning_effort=None,
        on_text=lambda _s: None,
        executor=executor,
        sampling=sampling,
    )
    assert _pview(executor.request.params) == {
        "max_tokens": 64,
        "temperature": 1.1,
        "top_p": 0.96,
        "presence_penalty": 1.5,
    }


def test_stream_model_without_sampling_sends_no_overrides():
    """With no Sampling set, js defers entirely to the backend/model default."""
    executor = _FakeExecutor(_text_events("ok"))
    model_client.stream_model(
        model_id="test",
        provider_id="openai",
        provider_base_url="http://localhost:11434/v1",
        provider_api_key="x",
        messages=[ai.user_message("hi")],
        tools=None,
        max_output_tokens=64,
        reasoning_effort=None,
        on_text=lambda _s: None,
        executor=executor,
    )
    assert _pview(executor.request.params) == {"max_tokens": 64}


def test_stream_model_sampling_topk_merges_with_provider_extra_body():
    """On the openai-compatible family top_k rides as RAW extra_body (the OpenAI
    protocol hard-rejects a TopKSamplerParams class) and merges with, not clobbers,
    an existing extra_body."""
    executor = _FakeExecutor(_text_events("ok"))
    model_client.stream_model(
        model_id="deepseek-chat",
        provider_id="deepseek",
        provider_base_url=None,
        provider_api_key="x",
        messages=[ai.user_message("hi")],
        tools=None,
        max_output_tokens=64,
        reasoning_effort=None,
        on_text=lambda _s: None,
        executor=executor,
        sampling=Sampling(top_k=40),
    )
    assert _pview(executor.request.params) == {
        "max_tokens": 64,
        "extra_body": {"top_k": 40, "max_reasoning_tokens": 32_000},
    }


def test_openai_compatible_topk_reppen_never_become_structured_params():
    """Regression: the openai-compatible family (ollama/llama.cpp/vLLM/deepseek)
    must forward top_k/repetition_penalty as raw extra_body, NEVER as
    TopK/RepetitionPenalty sampler classes — the OpenAIChatCompletionsProtocol
    those providers are pinned to raises ValueError the moment it sees them."""
    from ai.models.core import params as ap

    params = model_client._build_inference_params(
        Sampling(temperature=0.5, top_k=40, repetition_penalty=1.2, presence_penalty=0.3),
        "openai_compatible",
        reasoning=None,
        output=None,
        extra_body={},
    )
    samp = params.sampling
    assert ap.TopKSamplerParams not in samp
    rep = samp.get(ap.RepetitionPenaltyParams)
    assert rep is None or isinstance(rep.repetition_penalty, ap.ModelProviderDefault) or rep.repetition_penalty is None
    assert dict(params.extra_body) == {"top_k": 40, "repetition_penalty": 1.2}
    # temperature/presence_penalty stay top-level structured (the wire accepts them)
    assert ap.TemperatureSamplerParams in samp


def test_image_tool_result_becomes_user_file_message_without_persisting_base64(tmp_path):
    image = tmp_path / "img.png"
    image.write_bytes(b"\x89PNGfake")
    result = f"IMAGE_RESULT\t{image}\timage/png\tvisual stub"
    messages = model_client.build_tool_result_messages("tc1", "read_visual", result)
    assert [m.role for m in messages] == ["tool", "user"]
    tool_part = messages[0].parts[0]
    assert tool_part.kind == "tool_result"
    assert tool_part.tool_call_id == "tc1"
    assert tool_part.result == "visual stub"
    user_parts = messages[1].parts
    assert isinstance(user_parts[0], ai.types.messages.TextPart)
    assert user_parts[0].text == "visual stub"
    assert isinstance(user_parts[1], ai.types.messages.FilePart)
    assert user_parts[1].media_type == "image/png"
    assert isinstance(user_parts[1].data, bytes)

def test_plain_tool_result_message():
    msg = model_client.build_tool_result_message("tc1", "fs_search", "42")
    assert msg.role == "tool"
    assert msg.parts[0].kind == "tool_result"
    assert msg.parts[0].tool_call_id == "tc1"
    assert msg.parts[0].result == "42"
    assert msg.parts[0].tool_name == "fs_search"
    assert not msg.parts[0].is_error


def test_error_tool_result_message():
    msg = model_client.build_tool_result_message("tc1", "fs_search", "ERROR: disk full")
    assert msg.parts[0].is_error
    assert msg.parts[0].result == "ERROR: disk full"


def test_history_messages_can_be_roundtripped():
    """Provider-facing messages from history_to_ai_messages can be serialized and rebuilt."""
    msgs = [
        {"role": "assistant", "content": "hello", "reasoning_content": "hmm"},
        {"role": "tool", "tool_call_id": "tc1", "name": "fs_search", "content": "42"},
    ]
    out = model_client.history_to_ai_messages("sys", msgs)
    # Messages should survive pydantic serialization (used internally by ai)
    serialized = [m.model_dump() for m in out]
    assert len(serialized) == 3
    assert serialized[0]["role"] == "system"
    assert serialized[1]["role"] == "assistant"
    assert serialized[2]["role"] == "tool"
