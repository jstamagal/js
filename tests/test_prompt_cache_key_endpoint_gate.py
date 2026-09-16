"""`prompt_cache_key` goes to the endpoint, not to the SDK shape.

It is an OpenAI extension. `sdk="openai"` is the wire for ollama, vLLM,
llama.cpp, mimo, xAI and every `JS_BASE_URL` redirect, and an endpoint that
validates its inputs rejects the whole request rather than dropping a key it
does not know — NVIDIA answers
`400 Unsupported parameter(s): prompt_cache_key`. So the question these pin is
"where does this request land", which stays answerable after a provider id has
been pointed somewhere else.
"""

from __future__ import annotations

from js import providers
from js.model_client import accepts_prompt_cache_key


def _accepts(provider_id: str, base_url: str | None) -> bool:
    provider = providers.get_provider(provider_id)
    assert provider is not None, provider_id
    return accepts_prompt_cache_key(
        provider_name=provider.id.lower(),
        sdk_provider_name=(provider.effective_sdk_provider_id or "").lower(),
        base_url=base_url if base_url is not None else provider.default_base_url,
    )


def test_openai_on_its_own_endpoint_takes_the_key():
    # No default_base_url on the openai entries: the SDK fills in api.openai.com.
    assert _accepts("openai", None)
    assert _accepts("openai-responses", None)


def test_deepseek_takes_the_key():
    assert _accepts("deepseek", None)


def test_the_openai_provider_pointed_at_another_vendor_does_not():
    # The reported break: JS_PROVIDER=openai JS_BASE_URL=<nvidia> is an NVIDIA
    # request wearing the OpenAI SDK, and NVIDIA 400s the whole turn over it.
    assert not _accepts("openai", "https://integrate.api.nvidia.com/v1")


def test_an_openai_shaped_vendor_is_not_openai():
    # These ship sdk="openai" and their own host; none of them documented the key.
    assert not _accepts("mimo", None)
    assert not _accepts("xai", None)
    assert not _accepts("ollama-cloud", None)


def test_a_local_server_does_not_get_the_key():
    assert not _accepts("llama.cpp", None)
    assert not _accepts("vllm", None)


def test_a_url_that_does_not_parse_is_treated_as_foreign():
    assert not accepts_prompt_cache_key(
        provider_name="openai", sdk_provider_name="openai", base_url="http://[::1",
    )
