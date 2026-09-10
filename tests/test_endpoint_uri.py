"""Contract for `js -u model[:key][[shape]][[effort]]@url`."""

import pytest

from js.endpoint_uri import DUMMY_KEY, EndpointSpecError, parse


def test_bare_model_and_endpoint_uses_a_dummy_key_and_the_openai_wire():
    spec = parse("qwen27b@http://foo/v1")
    assert spec.model == "qwen27b"
    assert spec.api_key == DUMMY_KEY
    assert spec.base_url == "http://foo/v1"
    assert spec.provider_id == "openai-completions"
    assert spec.reasoning_effort is None


def test_explicit_key_is_taken_verbatim():
    assert parse("qwen27b:sk-no-key@http://foo/v1").api_key == "sk-no-key"


@pytest.mark.parametrize(
    "tag,provider_id",
    [
        ("openai", "openai-completions"),
        ("completions", "openai-completions"),
        ("responses", "openai-responses"),
        ("anthropic", "anthropic-custom"),
    ],
)
def test_each_wire_shape_selects_its_provider(tag, provider_id):
    assert parse(f"m[{tag}]@http://h/v1").provider_id == provider_id


def test_shape_and_effort_in_either_order():
    a = parse("claude-sonnet-5:sk-x[anthropic][max]@http://localhost:8317")
    b = parse("claude-sonnet-5:sk-x[max][anthropic]@http://localhost:8317")
    assert a == b
    assert a.provider_id == "anthropic-custom"
    assert a.reasoning_effort == "max"
    assert a.api_key == "sk-x"


def test_a_model_id_may_contain_slashes():
    spec = parse("antigravity/gemini-3.8-flash-high[xhigh]@http://vader:8317/v1")
    assert spec.model == "antigravity/gemini-3.8-flash-high"
    assert spec.reasoning_effort == "xhigh"


def test_last_colon_splits_the_key_so_a_tagged_model_id_survives():
    # ollama-style `name:tag`. Splitting on the FIRST colon would make the model
    # "qwen3" and the key "27b:sk-foo".
    spec = parse("qwen3:27b:sk-foo@http://foo/v1")
    assert spec.model == "qwen3:27b"
    assert spec.api_key == "sk-foo"


def test_key_containing_an_at_sign_does_not_steal_the_endpoint_split():
    spec = parse("m:sk-a@b@https://api.example.com/v1")
    assert spec.api_key == "sk-a@b"
    assert spec.base_url == "https://api.example.com/v1"


def test_settings_are_emitted_in_the_form_extra_accepts():
    pairs = parse("m:k[responses][high]@https://h/v1").as_settings()
    assert pairs == [
        "model.id=m",
        "provider.id=openai-responses",
        "provider.base_url=https://h/v1",
        "provider.api_key=k",
        "model.reasoning_effort=high",
    ]


def test_no_effort_tag_leaves_reasoning_effort_unset():
    assert all("reasoning_effort" not in p for p in parse("m@https://h/v1").as_settings())


@pytest.mark.parametrize(
    "bad,needle",
    [
        ("qwen27b", "no @endpoint"),
        ("qwen27b@foo", "no scheme"),
        ("@http://a/v1", "no model id"),
        ("m:@http://a/v1", "empty api key"),
        ("m[bogus]@http://a/v1", "unknown tag"),
        ("m[max][high]@http://a/v1", "two reasoning efforts"),
        ("m[openai][anthropic]@http://a/v1", "two wire shapes"),
        ("m[]@http://a/v1", "empty []"),
        ("", "empty endpoint spec"),
    ],
)
def test_bad_specs_say_what_is_wrong(bad, needle):
    with pytest.raises(EndpointSpecError) as exc:
        parse(bad)
    assert needle in str(exc.value)


def test_the_unknown_tag_error_lists_the_valid_tags():
    with pytest.raises(EndpointSpecError) as exc:
        parse("m[nope]@http://a/v1")
    message = str(exc.value)
    for valid in ("openai", "responses", "anthropic", "xhigh", "max"):
        assert valid in message
