"""Bracketed IPv6 hosts are URL hosts, not deprecated model annotations."""

import pytest

from js.endpoint_uri import EndpointSpecError, from_env, is_endpoint_form, parse, split


@pytest.mark.parametrize("base", ["http://[::1]:8080/v1", "https://[2001:db8::1]/v1"])
def test_plain_ipv6_base_url_is_not_endpoint_form(base):
    assert not is_endpoint_form(base)
    assert from_env({"JS_BASE_URL": base, "JS_MODEL": "model"}) is None
    assert split(base).base_url == base


@pytest.mark.parametrize("wire", ["", "openai+", "responses+", "anthropic+"])
def test_endpoint_model_and_key_with_ipv6_host(wire):
    spec = parse(wire + "http://Maker/Model:sk-TestKey@[::1]:8080/v1", env={})
    assert spec.model == "Maker/Model"
    assert spec.api_key == "sk-TestKey"
    assert spec.base_url == "http://[::1]:8080/v1"


@pytest.mark.parametrize("spec", ["http://model[openai]@host/v1", "model[openai]@http://host/v1"])
def test_legacy_model_annotations_remain_refused(spec):
    with pytest.raises(EndpointSpecError):
        split(spec)
