"""Prompt caching reaches each provider in the shape that provider accepts.

Every turn resends the whole conversation, so the prefix shared with the previous
turn is the bulk of each request. The three providers in use want three different
things, and the difference is invisible without checking what actually reaches the
wire: a bare CacheParams() is a working cache_control on Anthropic and nothing at
all on an OpenAI-compatible endpoint.

These drive the SDK's own request builders, so a change in how the SDK renders
caching surfaces here instead of silently costing money.
"""

from __future__ import annotations

import pytest
from ai.models.core import params as ai_params
from ai.providers.anthropic.protocol import _apply_anthropic_params
from ai.providers.openai.protocol import _apply_common_openai_params

from js.model_client import _build_inference_params
from js.sampling import Sampling


def _params(cache):
    return _build_inference_params(
        Sampling(), None, reasoning=None, output=None, extra_body={}, cache=cache,
    )


def _anthropic_wire(cache):
    api_kwargs: dict = {}
    _apply_anthropic_params(api_kwargs, _params(cache), provider="anthropic")
    return api_kwargs


def _openai_wire(cache, *, responses: bool = False):
    api_kwargs: dict = {}
    _apply_common_openai_params(
        api_kwargs, _params(cache), provider="openai", responses=responses,
    )
    return api_kwargs


def test_anthropic_receives_a_cache_control_breakpoint():
    # Anthropic caches only what a breakpoint covers, so silence caches nothing.
    assert _anthropic_wire(ai_params.CacheParams())["cache_control"] == {"type": "ephemeral"}


def test_an_openai_endpoint_receives_the_conversation_cache_key():
    wire = _openai_wire(ai_params.CacheParams(key="js-defaultagent-session01"))

    assert wire["prompt_cache_key"] == "js-defaultagent-session01"


def test_the_codex_responses_path_receives_the_same_key():
    wire = _openai_wire(ai_params.CacheParams(key="js-defaultagent-session01"), responses=True)

    assert wire["prompt_cache_key"] == "js-defaultagent-session01"


def test_a_bare_cache_params_sends_nothing_to_an_openai_endpoint():
    # The reason the shape is chosen per provider rather than shared: this is the
    # exact combination that looks configured and reaches the wire empty.
    assert "prompt_cache_key" not in _openai_wire(ai_params.CacheParams())


def test_anthropic_rejects_a_cache_key_outright():
    with pytest.raises(ValueError, match="cache key"):
        _anthropic_wire(ai_params.CacheParams(key="js-defaultagent-session01"))


def test_no_cache_params_leaves_the_request_untouched():
    assert _params(None) is None


def test_caching_does_not_disturb_the_other_request_params():
    params = _build_inference_params(
        Sampling(), None,
        reasoning=None,
        output=ai_params.OutputParams(max_tokens=64),
        extra_body={"custom": 1},
        cache=ai_params.CacheParams(key="k"),
    )

    assert params.output.max_tokens == 64
    assert params.extra_body == {"custom": 1}
    assert params.cache.key == "k"
