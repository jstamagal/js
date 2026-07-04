from __future__ import annotations

from typing import Any

import ai
import pytest

from js import model_client, providers
from js.model_client import ModelStreamResult
from js.sampling import Sampling


def _pview(p) -> dict:
    """Flatten an ai>=0.2.1 InferenceRequestParams to a plain dict for assertions."""
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


class _FakeProvider:
    async def aclose(self) -> None:
        pass


class _FakeModel:
    provider = _FakeProvider()


def _capture_stream_call(
    monkeypatch: pytest.MonkeyPatch,
    *,
    provider_id: str,
    model_id: str = "test-model",
    max_output_tokens: int | None = 64,
    reasoning_effort: str | None = None,
    sampling: Sampling | None = None,
    messages: list[ai.messages.Message] | None = None,
) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def fake_resolve_model(*args: Any, **kwargs: Any) -> _FakeModel:
        captured["resolve_args"] = args
        captured["resolve_kwargs"] = kwargs
        return _FakeModel()

    async def fake_stream_async(**kwargs: Any) -> ModelStreamResult:
        captured.update(kwargs)
        return ModelStreamResult(
            text="ok",
            tool_calls=[],
            reasoning="",
            usage=None,
            finish_reason="stop",
            assistant_message=ai.assistant_message("ok"),
        )

    monkeypatch.setattr(model_client, "resolve_model", fake_resolve_model)
    monkeypatch.setattr(model_client, "_stream_async", fake_stream_async)

    model_client.stream_model(
        model_id=model_id,
        provider_id=provider_id,
        provider_base_url=None,
        provider_api_key="test-key",
        messages=messages or [ai.user_message("hi")],
        tools=None,
        max_output_tokens=max_output_tokens,
        reasoning_effort=reasoning_effort,
        on_text=lambda _chunk: None,
        sampling=sampling,
    )
    return captured


def test_deepseek_reasoning_budget_uses_extra_body_when_enabled(monkeypatch):
    captured = _capture_stream_call(
        monkeypatch,
        provider_id="deepseek",
        model_id="deepseek-chat",
        reasoning_effort="high",
    )

    assert _pview(captured["params"]) == {
        "max_tokens": 64,
        "extra_body": {"max_reasoning_tokens": 32_000},
    }


def test_deepseek_reasoning_budget_is_omitted_when_reasoning_is_off(monkeypatch):
    captured = _capture_stream_call(
        monkeypatch,
        provider_id="deepseek",
        model_id="deepseek-chat",
        reasoning_effort="none",
    )

    assert _pview(captured["params"]) == {"max_tokens": 64}


def test_anthropic_wire_drops_openai_and_vllm_penalties(monkeypatch):
    captured = _capture_stream_call(
        monkeypatch,
        provider_id="opencode-go-anthropic",
        model_id="qwen3.7-plus",
        sampling=Sampling(
            temperature=0.7,
            top_p=0.9,
            top_k=50,
            repetition_penalty=1.1,
            presence_penalty=1.2,
        ),
    )

    assert _pview(captured["params"]) == {
        "max_tokens": 64,
        "temperature": 0.7,
        "top_p": 0.9,
        "top_k": 50,
    }


def test_openai_wire_emits_reasoning_effort_and_supported_sampling(monkeypatch):
    captured = _capture_stream_call(
        monkeypatch,
        provider_id="openai",
        model_id="gpt-5.1",
        reasoning_effort="medium",
        sampling=Sampling(
            temperature=0.7,
            top_p=0.9,
            top_k=50,
            repetition_penalty=1.1,
            presence_penalty=1.2,
        ),
    )

    assert _pview(captured["params"]) == {
        "max_tokens": 64,
        "reasoning_effort": "medium",
        "temperature": 0.7,
        "top_p": 0.9,
        "presence_penalty": 1.2,
    }


def test_openai_compatible_wire_keeps_reasoning_and_vllm_sampling(monkeypatch):
    captured = _capture_stream_call(
        monkeypatch,
        provider_id="opencode-go",
        model_id="glm-5.1",
        reasoning_effort="high",
        sampling=Sampling(
            temperature=0.7,
            top_p=0.9,
            top_k=50,
            repetition_penalty=1.1,
            presence_penalty=1.2,
        ),
    )

    assert _pview(captured["params"]) == {
        "max_tokens": 64,
        "reasoning_effort": "high",
        "temperature": 0.7,
        "top_p": 0.9,
        "presence_penalty": 1.2,
        # top_k/repetition_penalty ride as RAW extra_body — the OpenAI protocol this
        # wire is pinned to hard-rejects a TopK/RepetitionPenalty sampler class.
        "extra_body": {"top_k": 50, "repetition_penalty": 1.1},
    }


@pytest.mark.parametrize(
    ("provider_id", "model_id", "expected_params"),
    [
        (
            "deepseek",
            "deepseek-chat",
            {"max_tokens": 64, "extra_body": {"max_reasoning_tokens": 32_000}},
        ),
        ("mimo", "mimo-v2.5-pro", {"max_tokens": 64}),
        ("mimo-token-plan", "mimo-v2.5", {"max_tokens": 64}),
    ],
)
def test_append_only_providers_replay_history_without_rewriting_messages(
    monkeypatch,
    provider_id: str,
    model_id: str,
    expected_params: dict[str, Any],
):
    assert providers.provider_for_login(provider_id).append_only is True
    messages = model_client.history_to_ai_messages(
        "system",
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "read",
                            "arguments": '{"file_path":"a.txt"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "name": "read",
                "content": "ok",
            },
        ],
    )
    original_ids = [id(message) for message in messages]

    captured = _capture_stream_call(
        monkeypatch,
        provider_id=provider_id,
        model_id=model_id,
        messages=messages,
    )
    assert _pview(captured["params"]) == expected_params

    assert captured["messages"] is messages
    assert [id(message) for message in captured["messages"]] == original_ids
    assert [message.role for message in captured["messages"]] == [
        "system",
        "assistant",
        "tool",
    ]
    assert any(part.kind == "tool_call" for part in captured["messages"][1].parts)
