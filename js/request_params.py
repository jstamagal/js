"""A model request's parameters: sampling as each wire takes it, output and reasoning budgets, the prompt-cache key where the endpoint accepts one, and the fields a request sends."""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from urllib.parse import urlsplit

from ai.models.core import params as ai_params

from . import model_metadata, reasoning, routing, sampling as sampling_mod, settings as _settings
from .sampling import Sampling


def _sampler_map(values: dict) -> dict:
    """The sampling fields a wire takes as its own parameters, as ai>=0.2.1
    SamplerParamsMap entries (keyed by param class)."""
    out: dict = {}
    if "temperature" in values:
        out[ai_params.TemperatureSamplerParams] = ai_params.TemperatureSamplerParams(
            temperature=values["temperature"]
        )
    if "top_p" in values:
        out[ai_params.TopPSamplerParams] = ai_params.TopPSamplerParams(top_p=values["top_p"])
    if "top_k" in values:
        out[ai_params.TopKSamplerParams] = ai_params.TopKSamplerParams(top_k=values["top_k"])
    if "presence_penalty" in values:
        out[ai_params.RepetitionPenaltyParams] = ai_params.RepetitionPenaltyParams(
            presence_penalty=values["presence_penalty"])
    return out


def _build_inference_params(
    sampling: Sampling,
    standard: frozenset[str],
    *,
    reasoning: ai_params.ReasoningParams | None,
    output: ai_params.OutputParams | None,
    extra_body: dict[str, Any],
    cache: ai_params.CacheParams | None = None,
) -> ai_params.InferenceRequestParams | None:
    """Assemble an ``InferenceRequestParams`` from the parts, or None when there
    is nothing to send (so the provider keeps every default). The ``standard``
    sampling fields go as the wire's own parameters, every other set one in
    ``extra_body``; EXTRA_PARAMS (``extra_body``) wins a clash."""
    kwargs: dict[str, Any] = {}
    top, sampler_extra = sampling.split(standard)
    sampler_map = _sampler_map(top)
    if sampler_map:
        kwargs["sampling"] = sampler_map
    if reasoning is not None:
        kwargs["reasoning"] = reasoning
    if output is not None:
        kwargs["output"] = output
    if cache is not None:
        kwargs["cache"] = cache
    merged_extra = {**sampler_extra, **(extra_body or {})}
    if merged_extra:
        kwargs["extra_body"] = merged_extra
    if not kwargs:
        return None
    return ai_params.InferenceRequestParams(**kwargs)


# The sampling fields each wire's SDK takes as its own parameters; every other
# set field goes in ``extra_body`` under its own name, which puts it at the top
# of the request body all the same. The anthropic SDK's messages.stream() takes
# no sampling parameter at all (a TypeError before anything is sent), so the
# Anthropic wire sends every one through ``extra_body``.
_ANTHROPIC_SAMPLING: frozenset[str] = frozenset()


_RESPONSES_SAMPLING = frozenset({"temperature", "top_p"})


_CHAT_SAMPLING = frozenset({"temperature", "top_p", "presence_penalty"})


def build_request_params(
    *,
    model_id: str,
    provider_id: str | None,
    provider_base_url: str | None,
    max_output_tokens: int | None,
    reasoning_effort: str | None,
    thinking_budget: int | None = None,
    provider_extra: dict[str, Any] | None = None,
    sampling: Sampling | None = None,
    cache_key: str | None = None,
    settings: dict | None = None,
    wire: routing.Wire | None = None,
) -> ai_params.InferenceRequestParams | None:
    """The ``InferenceRequestParams`` one model call sends, or None when it
    sends none. The SDK translates each field to the provider's wire shape.
    ``wire`` is the route's `routing.Wire`, resolved here when not given.
    `wire_fields` reads back from it what went out."""
    # EXTRA_PARAMS is free-form raw passthrough. In the structured model every
    # raw kwarg rides extra_body, so fold a nested ``extra_body`` key up and carry
    # any sibling keys alongside it.
    extra_body: dict[str, Any] = {}
    _provider_extra = dict(provider_extra or {})
    _nested_extra_body = _provider_extra.pop("extra_body", None)
    if isinstance(_nested_extra_body, dict):
        extra_body.update(_nested_extra_body)
    extra_body.update(_provider_extra)
    if wire is None:
        wire = routing.resolve_wire(provider_id, base_url=provider_base_url, model_id=model_id)
    model_name = model_id.lower()
    sampling = sampling or Sampling()

    reasoning_params: ai_params.ReasoningParams | None = None
    if wire.anthropic:
        # The Anthropic wire requires max_tokens: OUTPUT_TOKENS_MAX when set,
        # else the model's known output limit, else ANTHROPIC_OUTPUT_TOKENS_MAX.
        # A catalog output limit as large as the context window is a
        # placeholder, not a cap: kimi-k3 lists 1048576 and its endpoint
        # answers "max_tokens must be at most 1000000".
        output_max_tokens = max_output_tokens
        if output_max_tokens is None:
            known = model_metadata.resolve_max_output(model_id, provider_id)
            window = model_metadata.context_window(model_id, provider_id)
            if known and window and known >= window:
                known = None
            output_max_tokens = known or _settings.knob(settings, "ANTHROPIC_OUTPUT_TOKENS_MAX")
        # A `thinking` set in EXTRA_PARAMS wins over the one js/reasoning.py plans.
        if wire.minimax:
            plan = reasoning.minimax_thinking(model_name, reasoning_effort, thinking_budget)
        else:
            plan = reasoning.anthropic_thinking(model_name, reasoning_effort, thinking_budget, settings=settings)
        if plan is not None:
            if plan.thinking is not None:
                extra_body.setdefault("thinking", plan.thinking)
            if plan.effort is not None:
                reasoning_params = ai_params.ReasoningParams(effort=plan.effort)
        thinking = extra_body.get("thinking")
        if isinstance(thinking, dict) and thinking.get("type") == "enabled":
            # The operator's own budget is in force. Budget thinking takes no
            # temperature or top_k: Anthropic answers either with a 400.
            sampling = replace(sampling, temperature=None, top_k=None)
        standard = _ANTHROPIC_SAMPLING
    else:
        output_max_tokens = max_output_tokens
        if reasoning_effort is not None:
            # The effort word as typed, snapped where js knows the model family
            # (js/reasoning.py). With no provider the word goes out as typed.
            allowed: frozenset[str] | None
            if wire.explicit_provider:
                allowed = reasoning.supported_efforts(model_name)
            else:
                allowed = None
            reasoning_params = ai_params.ReasoningParams(
                effort=reasoning.translate_effort(reasoning_effort, allowed))
        standard = _RESPONSES_SAMPLING if wire.responses else _CHAT_SAMPLING

    output_params = (
        ai_params.OutputParams(max_tokens=output_max_tokens)
        if output_max_tokens is not None
        else None
    )

    # DeepSeek's own reasoning budget, as an extra_body field. Gated on the WIRE,
    # not the model id: an endpoint that validates its inputs rejects the whole
    # request over a key it does not know (`nvidia/deepseek-ai/...`: 400
    # Unsupported parameter(s): `max_reasoning_tokens`).
    if wire.deepseek and reasoning.ladder_stop(reasoning_effort) != "none":
        extra_body.setdefault("max_reasoning_tokens", _settings.knob(settings, "DEEPSEEK_REASONING_TOKENS_MAX"))

    # Claude omits its thinking text unless the request asks for it. On the
    # Anthropic wire `reasoning.anthropic_thinking` asks with display=summarized;
    # through CLIProxyAPI's OpenAI-shaped endpoint the ask is include_reasoning,
    # which the proxy maps onto the same display.
    if (wire.transport == "cliproxyapi"
            and _settings.knob(settings, "CLAUDE_PROXY_INCLUDE_REASONING")):
        extra_body.setdefault("include_reasoning", True)

    template_kwargs = _settings.knob(settings, "TEMPLATE_KWARGS")
    if template_kwargs:
        extra_body.setdefault("chat_template_kwargs", sampling_mod.parse_template_kwargs(template_kwargs))

    # Prompt caching. Every turn resends the whole conversation, so the prefix it
    # shares with the previous turn is the bulk of each request, and the three
    # providers in use want three different things:
    #   Anthropic caches only what a cache_control breakpoint covers, so the
    #     request has to ask. Direct Anthropic uses automatic caching; compatible
    #     providers get an explicit message breakpoint covering the whole prefix.
    #   OpenAI-compatible endpoints cache long prefixes automatically. There is no
    #     breakpoint to set, but prompt_cache_key pins a conversation to the machine
    #     holding its prefix, which is what makes the hit rate hold up across turns.
    #   DeepSeek caches on disk automatically from token zero, with no request-side
    #     control at all, so it takes the same key harmlessly.
    # `prompt_cache_key` is an OpenAI extension, not part of the OpenAI-compatible
    # shape everyone implements, so what decides it is which ENDPOINT is being
    # called, not which SDK shape talks to it. `sdk="openai"` is the wire for
    # ollama, vLLM, llama.cpp, mimo, xAI and every `JS_BASE_URL` redirect, none of
    # which are OpenAI; asking the wire sent the key to all of them, and an
    # endpoint that validates its inputs rejects the whole request rather than
    # dropping a key it does not know (observed:
    # `JS_PROVIDER=openai JS_BASE_URL=https://integrate.api.nvidia.com/v1`, 400
    # `Unsupported parameter(s): prompt_cache_key`). Ask the host instead. Losing
    # the hint on an unlisted endpoint costs cache hit rate; sending it costs the
    # entire request.
    accepts_cache_key = accepts_prompt_cache_key(
        provider_name=wire.provider_name,
        sdk_provider_name=wire.sdk_provider_name,
        base_url=provider_base_url,
    )
    if wire.anthropic and _settings.knob(settings, "ANTHROPIC_PROMPT_CACHE"):
        cache_params = ai_params.CacheParams()
    elif cache_key and accepts_cache_key:
        cache_params = ai_params.CacheParams(key=cache_key)
    else:
        cache_params = None

    return _build_inference_params(
        sampling,
        standard,
        reasoning=reasoning_params,
        output=output_params,
        extra_body=extra_body,
        cache=cache_params,
    )


def wire_fields(params: ai_params.InferenceRequestParams | None) -> dict[str, Any]:
    """What a request built by `build_request_params` carries for its output
    cap and thinking, read back from it: ``max_out``, ``effort`` (the word
    sent), ``thinking`` (a disabled or budgeted ``thinking`` object), and
    ``DEEPSEEK_REASONING_TOKENS_MAX``. A field the request does not carry is
    absent."""
    out: dict[str, Any] = {}
    if params is None:
        return out
    output = params.output
    if isinstance(output, ai_params.OutputParams) and isinstance(output.max_tokens, int):
        out["max_out"] = output.max_tokens
    reasoning_params = params.reasoning
    if isinstance(reasoning_params, ai_params.ReasoningParams) and isinstance(reasoning_params.effort, str):
        out["effort"] = reasoning_params.effort
    extra = params.extra_body or {}
    thinking = extra.get("thinking")
    if isinstance(thinking, dict):
        if thinking.get("type") == "disabled":
            out["thinking"] = "disabled"
        elif isinstance(thinking.get("budget_tokens"), int):
            out["thinking"] = f"budget:{thinking['budget_tokens']}"
    if "max_reasoning_tokens" in extra:
        out["DEEPSEEK_REASONING_TOKENS_MAX"] = extra["max_reasoning_tokens"]
    return out


# The two vendors that document `prompt_cache_key`: OpenAI, and DeepSeek, whose
# automatic disk cache takes the key harmlessly. Hosts, because that is the only
# thing that stays true when a provider id is pointed at somebody else's server.
_CACHE_KEY_HOSTS = frozenset({"api.openai.com", "api.deepseek.com"})


# The provider ids that reach one of those hosts by default, i.e. when nothing
# overrode the base URL. OpenAI's own entries carry no `default_base_url` — the
# SDK fills in api.openai.com — so there is no URL to match against for them.
_CACHE_KEY_VENDORS = frozenset({"openai", "openai-completions", "openai-responses", "deepseek"})


# opencode-go routes and caches by conversation, keyed on this header. Its
# Anthropic endpoint refuses a request without it: 400 MissingSessionID,
# "Request is missing x-opencode-session and cannot be routed efficiently".
# A request with no session key (a session-less run, a compaction summary)
# gets a one-off id.
OPENCODE_GO_PROVIDERS = frozenset({"opencode-go", "opencode-go-anthropic"})


OPENCODE_SESSION_HEADER = "x-opencode-session"


def accepts_prompt_cache_key(
    *, provider_name: str, sdk_provider_name: str, base_url: str | None
) -> bool:
    """Does the endpoint this request is actually going to take `prompt_cache_key`?

    A provider id names a vendor only until someone points it elsewhere; the base
    URL is what says where the request lands. So a redirected `openai` provider
    answers no, and a `sdk="openai"` provider serving its own vendor's host
    (ollama, vLLM, mimo, xAI) was never OpenAI to begin with.
    """
    if base_url:
        try:
            host = (urlsplit(base_url).hostname or "").lower()
        except ValueError:
            return False
        return host in _CACHE_KEY_HOSTS
    return provider_name in _CACHE_KEY_VENDORS or sdk_provider_name in _CACHE_KEY_VENDORS
