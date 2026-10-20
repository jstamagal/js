"""Tests for the ai-python SDK boundary in js/model_client.py.

No network calls. All model interactions use a public ``ai.models.Stream``.
"""

from __future__ import annotations

from types import SimpleNamespace

import ai
import ai.types.events
import ai.types.messages
import ai.types.tools
import ai.types.usage
import ai.models
import pytest

from js import model_client
from js.sampling import Sampling


class _FakeStreamFactory:
    """Returns a public SDK stream over scripted events and captures inputs."""

    def __init__(self, events):
        self.events = events
        self.kwargs = None

    def __call__(self, **kwargs):
        self.kwargs = kwargs

        async def generate():
            for event in self.events:
                yield event

        return ai.models.Stream(generate())


def _use_fake_stream(monkeypatch, events) -> _FakeStreamFactory:
    factory = _FakeStreamFactory(events)
    monkeypatch.setattr(model_client, "_open_stream", factory)
    return factory


@pytest.mark.parametrize("max_output_tokens", [None, 64])
def test_stream_model_optional_params_reaches_sdk(monkeypatch, max_output_tokens):
    factory = _FakeStreamFactory(_text_events("ok"))
    # Exercise _stream_async -> _open_stream; replace only the SDK call.
    monkeypatch.setattr(ai, "stream", factory)
    emitted = []
    result = model_client.stream_model(
        model_id="test",
        provider_id="openai",
        provider_base_url="http://llamacpp.test/v1",
        provider_api_key="fixture",
        messages=[ai.user_message("hi")],
        tools=None,
        max_output_tokens=max_output_tokens,
        reasoning_effort=None,
        on_text=emitted.append,
    )
    assert result.text == "ok"
    assert emitted == ["ok"]
    if max_output_tokens is None:
        assert factory.kwargs["params"] is None
    else:
        assert _pview(factory.kwargs["params"]) == {"max_tokens": max_output_tokens}


def test_sync_stream_finalizes_generator_before_event_loop_shutdown(monkeypatch):
    finalized = False

    async def generate():
        nonlocal finalized
        try:
            for event in _text_events("done"):
                yield event
        finally:
            finalized = True

    def open_stream(**_kwargs):
        return ai.models.Stream(generate())

    monkeypatch.setattr(model_client, "_open_stream", open_stream)
    result = model_client.stream_model(
        model_id="test",
        provider_id="openai",
        provider_base_url="http://localhost:11434/v1",
        provider_api_key="x",
        messages=[ai.user_message("hi")],
        tools=None,
        max_output_tokens=64,
        reasoning_effort=None,
        on_text=lambda _text: None,
    )

    assert result.text == "done"
    assert finalized


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


def _assert_provider_recovery(message: str, *, provider: str, command: str) -> None:
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

    # An explicit base URL is a deliberate endpoint choice: it is honoured as an
    # OpenAI-compatible endpoint with the given key, never the SDK gateway.
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "gw-decoy")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-decoy")
    result = model_client.resolve_model(
        "deepseek/deepseek-v4-flash",
        provider_id=None,
        provider_base_url="http://127.0.0.1:9/v1",
        provider_api_key="sk-explicit",
        provider_headers={"X-Test": "1"},
    )
    assert "model_id" not in captured
    assert result.id == "deepseek/deepseek-v4-flash"
    assert result.provider.base_url == "http://127.0.0.1:9/v1"
    assert result.provider.api_key == "sk-explicit"
    assert result.provider.headers.get("X-Test") == "1"
    assert "gateway" not in type(result.provider).__name__.lower()
    assert isinstance(result.provider.protocol_override, model_client._ReasoningContentProtocol)

    # No key given: keyless local endpoint, never the env decoy.
    keyless = model_client.resolve_model(
        "local-model", provider_id=None, provider_base_url="http://127.0.0.1:9/v1", provider_api_key=None,
    )
    assert keyless.provider.base_url == "http://127.0.0.1:9/v1"
    assert keyless.provider.api_key == ""


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
    _assert_provider_recovery(message, provider="openai", command="js --login openai")
    assert "set API_KEY <value>" in message


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
    _assert_provider_recovery(message, provider="stale", command="js --login stale")
    assert "js --list-models" in message


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
    _assert_provider_recovery(message, provider="openai", command="js --login openai")
    assert "set API_KEY <value>" in message
    assert "Incorrect API key" in message


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


def test_stream_model_collects_text_reasoning_tool_calls_and_usage(monkeypatch):
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
    factory = _use_fake_stream(monkeypatch, events)
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
    )
    assert result.text == "DONE"
    assert result.reasoning == "thinking"
    assert result.finish_reason == "tool_calls"
    assert len(result.tool_calls) == 1
    tc = result.tool_calls[0]
    assert tc.id == "tc1"
    assert result.usage.output_tokens == 10
    assert emitted_text == ["DONE"]
    assert factory.kwargs is not None
    assert _pview(factory.kwargs["params"]) == {"max_tokens": 100}


def test_stream_model_passes_reasoning_effort(monkeypatch):
    factory = _use_fake_stream(monkeypatch, _text_events("ok"))
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
    )
    assert _pview(factory.kwargs["params"]) == {"max_tokens": 64, "reasoning_effort": "low"}


def test_stream_model_passes_reasoning_none(monkeypatch):
    factory = _use_fake_stream(monkeypatch, _text_events("ok"))
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
    )
    assert _pview(factory.kwargs["params"]) == {"max_tokens": 64}


def test_stream_model_asks_for_claude_thinking_on_the_openai_wire(monkeypatch):
    # Claude hides its thinking unless the request asks for it, so a Claude
    # model behind an OpenAI-shaped proxy showed and kept no reasoning at all.
    factory = _use_fake_stream(monkeypatch, _text_events("ok"))
    model_client.stream_model(
        model_id="claude-opus-5-5",
        provider_id="openai",
        provider_base_url="http://localhost:8999/v1",
        provider_api_key="x",
        messages=[ai.user_message("hi")],
        tools=None,
        max_output_tokens=64,
        reasoning_effort=None,
        on_text=lambda _s: None,
    )
    assert _pview(factory.kwargs["params"]) == {"max_tokens": 64, "extra_body": {"include_reasoning": True}}


def test_stream_model_puts_deepseek_reasoning_budget_in_extra_body(monkeypatch):
    factory = _use_fake_stream(monkeypatch, _text_events("ok"))
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
    )
    assert _pview(factory.kwargs["params"]) == {"max_tokens": 64, "extra_body": {"max_reasoning_tokens": 32_000}}


def test_stream_model_strips_minimax_reasoning_by_model_prefix(monkeypatch):
    factory = _use_fake_stream(monkeypatch, _text_events("ok"))
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
    )
    assert _pview(factory.kwargs["params"]) == {"max_tokens": 64}



def test_stream_model_applies_sampling_from_env_for_openai(monkeypatch):
    """JS_* env sampling is typed before stream_model; OpenAI only receives supported knobs."""
    sampling = Sampling.from_env(
        {
            "JS_TEMPERATURE": "1.1",
            "JS_TOP_P": "0.96",
            "JS_TOP_K": "64",
            "JS_REPETITION_PENALTY": "1.05",
            "JS_PRESENCE_PENALTY": "1.5",
        }
    )
    factory = _use_fake_stream(monkeypatch, _text_events("ok"))
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
        sampling=sampling,
    )
    assert _pview(factory.kwargs["params"]) == {
        "max_tokens": 64,
        "temperature": 1.1,
        "top_p": 0.96,
        "presence_penalty": 1.5,
    }


def test_stream_model_sampling_topk_merges_with_provider_extra_body(monkeypatch):
    """On the openai-compatible family top_k rides as RAW extra_body (the OpenAI
    protocol hard-rejects a TopKSamplerParams class) and merges with, not clobbers,
    an existing extra_body."""
    factory = _use_fake_stream(monkeypatch, _text_events("ok"))
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
        sampling=Sampling(top_k=40),
    )
    assert _pview(factory.kwargs["params"]) == {
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
    # The stub goes once, in the tool message; the media message has the label.
    assert user_parts[0].text == model_client.media_label("tc1", "visual stub")
    assert isinstance(user_parts[1], ai.types.messages.FilePart)
    assert user_parts[1].media_type == "image/png"
    assert isinstance(user_parts[1].data, bytes)

def test_each_media_message_of_a_batch_names_its_own_tool_call(tmp_path):
    # A batch's media messages all follow its tool messages, so each one must
    # say which call it belongs to.
    labels = []
    for call_id, name in (("call_a", "a.png"), ("call_b", "b.png")):
        image = tmp_path / name
        image.write_bytes(b"\x89PNGfake")
        marker = f"IMAGE_RESULT\t{image}\timage/png\tVISUAL_FILE {image} mime=image/png"
        user = model_client.build_tool_result_messages(call_id, "read", marker)[1]
        labels.append(user.parts[0].text)
    assert "call_a" in labels[0] and "a.png" in labels[0]
    assert "call_b" in labels[1] and "b.png" in labels[1]


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

    # Pin the conversion itself first. A pure round-trip proves only that every
    # element is serializable, which an empty list satisfies perfectly.
    roles = [m.role for m in out]
    assert roles == ["system", "assistant", "tool"]
    assert any("sys" in str(part) for part in out[0].parts)
    assert any("hello" in str(part) for part in out[1].parts)
    assert any("42" in str(part) for part in out[2].parts)

    # Messages should survive pydantic serialization (used internally by ai)
    serialized = [m.model_dump() for m in out]
    rebuilt = [ai.types.messages.Message.model_validate(dump) for dump in serialized]
    assert rebuilt == out


@pytest.mark.parametrize("target", ["reasoning.anthropic_thinking", "_build_inference_params", "stream_transport.begin_call"])
def test_provider_is_closed_when_request_setup_fails(monkeypatch, target):
    """The provider owns an HTTP client from resolve_model on; a failure while
    the request is assembled must still close it (ALL-CLAIMS model_client MED)."""
    import asyncio
    from types import SimpleNamespace

    class Provider:
        closed = False

        async def aclose(self):
            Provider.closed = True

    def fail(*_args, **_kwargs):
        raise ValueError("forced setup failure")

    monkeypatch.setattr(model_client, "resolve_model", lambda *a, **k: SimpleNamespace(provider=Provider()))
    owner_name, _, attr = target.rpartition(".")
    owner = getattr(model_client, owner_name) if owner_name else model_client
    monkeypatch.setattr(owner, attr, fail)
    with pytest.raises(ValueError, match="forced setup failure"):
        asyncio.run(model_client.stream_model_async(
            model_id="claude-opus-4", provider_id="anthropic", provider_base_url=None,
            provider_api_key="test", messages=[], tools=None, max_output_tokens=4096,
            reasoning_effort="high", on_text=lambda _text: None,
        ))
    assert Provider.closed


def test_extra_headers_go_on_the_turn_request_over_the_login_headers(tmp_path):
    from js import runtime, turn_settings
    from js.config import Config

    cfg = Config(
        agent_id="a", agent_dir=tmp_path, model="m", provider_id=None,
        provider_base_url="http://x", provider_api_key=None, reasoning_effort=None,
        debug_log=None, history_file=tmp_path / "h", sessions_dir=tmp_path,
        session_file=tmp_path / "s.jsonl", prompts_dir=tmp_path,
        provider_headers={"x-pool": "login", "X-Keep": "1"},
        **turn_settings.project({"EXTRA_HEADERS": "X-Pool: fofa; X-JS-Scrub: on"}),
    )
    request = runtime._turn_request(
        cfg, model_override=None, provider_id_override=None,
        provider_base_url_override=None, provider_api_key_override=None,
        reasoning_effort_override=runtime._UNSET, max_output_override=1, sampling=None,
    )
    assert request.headers == {"X-Keep": "1", "X-Pool": "fofa", "X-JS-Scrub": "on"}


def test_set_refuses_extra_headers_without_a_colon():
    from js import settings

    value, error = settings.coerce_value(settings.SPEC_BY_KEY["EXTRA_HEADERS"], "X-Ok: 1; broken")
    assert value is None and error


@pytest.mark.parametrize("raw", ["X-Pool: fофа", "X(Pool): 1", "X-A: 1\rX-B: 2"])
def test_set_refuses_extra_headers_a_request_cannot_carry(raw):
    from js import settings

    value, error = settings.coerce_value(settings.SPEC_BY_KEY["EXTRA_HEADERS"], raw)
    assert value is None and error


def test_paranoid_mode_extra_sends_the_scrub_header(tmp_path):
    from js import runtime, turn_settings
    from js.config import Config

    cfg = Config(
        agent_id="a", agent_dir=tmp_path, model="m", provider_id=None,
        provider_base_url="http://x", provider_api_key=None, reasoning_effort=None,
        debug_log=None, history_file=tmp_path / "h", sessions_dir=tmp_path,
        session_file=tmp_path / "s.jsonl", prompts_dir=tmp_path,
        **turn_settings.project({"PARANOID_MODE": "extra"}),
    )
    request = runtime._turn_request(
        cfg, model_override=None, provider_id_override=None,
        provider_base_url_override=None, provider_api_key_override=None,
        reasoning_effort_override=runtime._UNSET, max_output_override=1, sampling=None,
    )
    assert request.headers == {"X-JS-Scrub": "on"}


def _cpa_chunk(delta: dict, finish: str | None = None):
    from openai.types.chat import ChatCompletionChunk

    return ChatCompletionChunk.model_validate({
        "id": "c1", "object": "chat.completion.chunk", "created": 0, "model": "claude",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    })


def test_streamed_reasoning_block_fragments_join_by_index():
    collected = model_client.ReasoningBlocks()
    for delta in (
        {"reasoning_blocks": [{"index": 0, "type": "thinking", "thinking": "Let me "}]},
        {"reasoning_blocks": [{"index": 0, "type": "thinking", "thinking": "think."}]},
        {"reasoning_blocks": [{"index": 0, "type": "thinking", "signature": "sig-"},
                              {"index": 1, "type": "redacted_thinking", "data": "opaque"}]},
        {"reasoning_blocks": [{"index": 0, "type": "thinking", "signature": "123"}]},
    ):
        collected.observe(_cpa_chunk(delta))
    assert collected.blocks() == [
        {"type": "thinking", "thinking": "Let me think.", "signature": "sig-123"},
        {"type": "redacted_thinking", "data": "opaque"},
    ]


def test_a_non_streaming_reply_gives_its_blocks_whole():
    collected = model_client.ReasoningBlocks()
    collected.observe({"choices": [{"message": {"content": "hi", "reasoning_blocks": [
        {"index": 0, "type": "thinking", "thinking": "t", "signature": "s"}]}}]})
    assert collected.blocks() == [{"type": "thinking", "thinking": "t", "signature": "s"}]


async def _wire_messages(protocol, history: list[dict], chunks=()):
    sent = {}

    async def create(**kwargs):
        sent.update(kwargs)

        async def gen():
            for chunk in chunks:
                yield chunk
        return gen()

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    model = ai.Model(id="claude", provider=ai.get_provider("openai", base_url="http://x/v1", api_key="k"))
    messages = model_client.history_to_ai_messages("sys", history, provider_id=None, model_id="claude")
    async for _event in protocol.stream(client, model, messages, provider="openai"):
        pass
    return sent["messages"]


_BLOCKS = [{"type": "thinking", "thinking": "plan", "signature": "sig"}]
_FROM = {"provider": None, "model": "claude"}
_HISTORY = [
    {"role": "user", "content": "go"},
    {"role": "assistant", "content": "", "reasoning_content": "plan", "reasoning_blocks": _BLOCKS,
     "reasoning_from": _FROM,
     "tool_calls": [{"id": "t1", "type": "function", "function": {"name": "read", "arguments": "{}"}}]},
    {"role": "tool", "tool_call_id": "t1", "name": "read", "content": "ok"},
]


def test_a_cpa_route_sends_stored_blocks_back_instead_of_reasoning_content():
    import asyncio

    sent = asyncio.run(_wire_messages(model_client._ReasoningContentProtocol(reasoning_blocks=True), _HISTORY))
    assistant = next(m for m in sent if m["role"] == "assistant")
    assert assistant["reasoning_blocks"] == _BLOCKS
    assert "reasoning_content" not in assistant and "reasoning" not in assistant


def test_blocks_are_never_sent_off_cpa():
    import asyncio

    sent = asyncio.run(_wire_messages(model_client._ReasoningContentProtocol(), _HISTORY))
    assistant = next(m for m in sent if m["role"] == "assistant")
    assert "reasoning_blocks" not in assistant
    assert assistant["reasoning_content"] == "plan"


def test_only_a_cliproxyapi_route_turns_block_echo_on():
    cpa = model_client.resolve_model("claude", provider_id="cliproxyapi",
                                      provider_base_url="http://vader:8999/v1", provider_api_key="k")
    local = model_client.resolve_model("m", provider_id="ollama",
                                        provider_base_url="http://localhost:11434/v1", provider_api_key="k")
    assert cpa.provider.protocol.reasoning_blocks is True
    assert getattr(local.provider.protocol, "reasoning_blocks", False) is False


def test_the_reply_blocks_reach_the_stream_result(monkeypatch):
    chunks = [
        _cpa_chunk({"content": "hi", "reasoning_blocks": [{"index": 0, "type": "thinking", "thinking": "x"}]}),
        _cpa_chunk({"reasoning_blocks": [{"index": 0, "type": "thinking", "signature": "s"}]}, finish="stop"),
    ]

    async def create(**kwargs):
        async def gen():
            for chunk in chunks:
                yield chunk
        return gen()

    real_client = model_client._sdk_client
    model = model_client.resolve_model("claude", provider_id="cliproxyapi",
                                       provider_base_url="http://vader:8999/v1", provider_api_key="k")
    sdk = real_client(model)
    monkeypatch.setattr(sdk.chat.completions, "create", create)
    monkeypatch.setattr(model_client, "resolve_model", lambda *a, **k: model)
    result = model_client.stream_model(
        model_id="claude", provider_id="cliproxyapi", provider_base_url="http://vader:8999/v1",
        provider_api_key="k", messages=[ai.user_message("hi")], tools=None, max_output_tokens=None,
        reasoning_effort=None, on_text=lambda _c: None,
    )
    assert result.reasoning_blocks == [{"type": "thinking", "thinking": "x", "signature": "s"}]


def test_a_turn_stores_the_reply_blocks_on_its_assistant_message(monkeypatch, tmp_path):
    import ai.types.usage

    from js import runtime
    from test_lazy_tool_discovery import _cfg

    blocks = [{"type": "thinking", "thinking": "x", "signature": "s"}]

    async def fake_stream(**_kwargs):
        return model_client.ModelStreamResult(
            text="done", tool_calls=[], reasoning="x",
            usage=ai.types.usage.Usage(input_tokens=1, output_tokens=1), finish_reason="stop",
            assistant_message=model_client.with_reasoning_blocks(ai.assistant_message("done"), blocks),
            reasoning_blocks=blocks,
        )

    monkeypatch.setattr(runtime.model_client, "stream_model_async", fake_stream)
    messages = [{"role": "user", "content": "go"}]
    runtime.run_turn(_cfg(tmp_path), "sys", messages, runtime.Telemetry(None), suppress_output=True)
    assert messages[-1]["reasoning_blocks"] == blocks


def test_compaction_drops_reasoning_blocks_with_the_signed_reasoning():
    from js import memory

    messages = [{"role": "assistant", "content": "a", "reasoning_content": "x",
                 "reasoning_blocks": [{"type": "thinking", "thinking": "x", "signature": "s"}]}]
    assert memory.drop_signed_reasoning(messages) == 1
    assert messages == [{"role": "assistant", "content": "a", "reasoning_content": "x"}]


def test_dropping_signed_reasoning_leaves_no_blocks_to_replay(monkeypatch, tmp_path):
    import ai.types.usage

    from js import memory, runtime
    from test_lazy_tool_discovery import _cfg

    blocks = [{"type": "thinking", "thinking": "x", "signature": "s"}]

    async def fake_stream(**_kwargs):
        return model_client.ModelStreamResult(
            text="done", tool_calls=[], reasoning="x",
            usage=ai.types.usage.Usage(input_tokens=1, output_tokens=1), finish_reason="stop",
            assistant_message=model_client.with_reasoning_blocks(ai.assistant_message("done"), blocks),
            reasoning_blocks=blocks,
        )

    monkeypatch.setattr(runtime.model_client, "stream_model_async", fake_stream)
    messages = [{"role": "user", "content": "go"}]
    runtime.run_turn(_cfg(tmp_path), "sys", messages, runtime.Telemetry(None), suppress_output=True)
    memory.drop_signed_reasoning(messages)

    rebuilt = model_client.history_to_ai_messages("sys", messages)
    assert all(model_client._stored_reasoning_blocks(m) is None for m in rebuilt)


def test_blocks_go_back_on_their_own_message_when_answers_repeat():
    import asyncio

    history = [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "Done.", "reasoning_content": "first"},
        {"role": "user", "content": "two"},
        {"role": "assistant", "content": "Done.", "reasoning_content": "second", "reasoning_blocks": _BLOCKS,
         "reasoning_from": _FROM},
        {"role": "user", "content": "three"},
    ]
    sent = asyncio.run(_wire_messages(model_client._ReasoningContentProtocol(reasoning_blocks=True), history))
    first, second = [m for m in sent if m["role"] == "assistant"]
    assert "reasoning_blocks" not in first
    assert second["reasoning_blocks"] == _BLOCKS


def test_reasoning_blocks_count_in_both_token_estimates():
    from js import context_budget

    blocks = [{"type": "thinking", "thinking": "t" * 4000, "signature": "s" * 40000}]
    plain = {"role": "assistant", "content": "done", "reasoning_content": "t" * 4000}
    signed = {**plain, "reasoning_blocks": blocks}

    history_plain = context_budget.estimate_message_tokens(plain)
    history_signed = context_budget.estimate_message_tokens(signed)
    sdk_plain, = model_client.history_to_ai_messages("", [plain])
    sdk_signed = model_client.with_reasoning_blocks(sdk_plain, blocks)

    assert history_signed >= history_plain + 10000
    assert (context_budget.estimate_message_tokens(sdk_signed)
            >= context_budget.estimate_message_tokens(sdk_plain) + 10000)


def test_blocks_go_back_only_to_the_model_that_wrote_them(monkeypatch, tmp_path):
    import ai.types.usage

    from js import runtime
    from test_lazy_tool_discovery import _cfg

    blocks = [{"type": "thinking", "thinking": "x", "signature": "s"}]

    async def fake_stream(**_kwargs):
        return model_client.ModelStreamResult(
            text="done", tool_calls=[], reasoning="x",
            usage=ai.types.usage.Usage(input_tokens=1, output_tokens=1), finish_reason="stop",
            assistant_message=model_client.with_reasoning_blocks(ai.assistant_message("done"), blocks),
            reasoning_blocks=blocks,
        )

    monkeypatch.setattr(runtime.model_client, "stream_model_async", fake_stream)
    cfg = _cfg(tmp_path)
    messages = [{"role": "user", "content": "go"}]
    runtime.run_turn(cfg, "sys", messages, runtime.Telemetry(None), suppress_output=True)

    same = model_client.history_to_ai_messages("", messages, provider_id=cfg.provider_id, model_id=cfg.model)
    other = model_client.history_to_ai_messages("", messages, provider_id=cfg.provider_id, model_id="other-model")
    assert model_client._stored_reasoning_blocks(same[-1]) == blocks
    assert model_client._stored_reasoning_blocks(other[-1]) is None


def _sent_headers(monkeypatch, *, provider_id, base, headers):
    """The headers of the one request a turn sends with ``headers``."""
    import asyncio
    import json

    import httpx2

    seen = []

    def respond(request):
        seen.append(request.headers)
        if provider_id == "anthropic":
            events = [
                {"type": "message_start", "message": {"id": "m", "type": "message", "role": "assistant",
                 "model": "claude", "content": [], "stop_reason": None,
                 "usage": {"input_tokens": 1, "output_tokens": 0}}},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}},
                {"type": "message_stop"},
            ]
            body = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
        else:
            chunk = {"id": "x", "object": "chat.completion.chunk", "created": 1, "model": "m",
                     "choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}]}
            body = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"
        return httpx2.Response(200, headers={"Content-Type": "text/event-stream"}, content=body.encode())

    async def drive():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as client:
            get_provider = model_client.ai.get_provider
            monkeypatch.setattr(model_client.ai, "get_provider",
                                lambda *a, **k: get_provider(*a, **k, client=client))
            await model_client.stream_model_async(
                model_id="claude" if provider_id == "anthropic" else "m", provider_id=provider_id,
                provider_base_url=base, provider_api_key="login-key", provider_headers=headers,
                messages=[ai.user_message("hi")], tools=None, max_output_tokens=64,
                reasoning_effort=None, on_text=lambda _c: None)

    asyncio.run(drive())
    assert len(seen) == 1
    return seen[0]


def test_an_extra_authorization_header_never_doubles_the_logins(monkeypatch):
    sent = _sent_headers(monkeypatch, provider_id=None, base="http://offline.invalid/v1",
                         headers={"authorization": "Bearer extra", "X-Extra": "1"})

    assert sent.get_list("authorization") == ["Bearer login-key"]
    assert sent.get_list("x-extra") == ["1"]


def test_an_extra_anthropic_version_reaches_the_request(monkeypatch):
    sent = _sent_headers(monkeypatch, provider_id="anthropic", base=None,
                         headers={"anthropic-version": "2099-01-01"})

    assert sent.get_list("anthropic-version") == ["2099-01-01"]
    assert sent.get_list("x-api-key") == ["login-key"]


_FRAMING = {"Content-Length": "1", "Content-Type": "text/plain", "Transfer-Encoding": "chunked",
            "Host": "elsewhere.invalid"}


@pytest.mark.parametrize(("provider_id", "base"), [(None, "http://offline.invalid/v1"), ("anthropic", None)])
def test_configured_framing_headers_never_reach_the_request(monkeypatch, provider_id, base):
    sent = _sent_headers(monkeypatch, provider_id=provider_id, base=base, headers=dict(_FRAMING))

    assert sent.get_list("content-length") != ["1"]
    assert sent.get_list("content-type") == ["application/json"]
    assert sent.get_list("transfer-encoding") == []
    assert "elsewhere.invalid" not in sent.get_list("host")
