"""Endpoint transport schemes are case-insensitive without rewriting URL data."""

import pytest

from js.endpoint_uri import EndpointSpecError, split


@pytest.mark.parametrize("scheme", ["HTTP", "Http", "HTTPS", "hTtPs"])
@pytest.mark.parametrize("wire", ["", "openai+", "responses+", "anthropic+"])
def test_endpoint_scheme_case_preserves_endpoint_components(scheme, wire):
    suffix = "://Maker/Model:free:sk-MixedCaseKey@Box:11434/Path/v1?name=Mixed#Frag"
    result = split(wire + scheme + suffix)
    expected = split(wire + scheme.lower() + suffix)
    assert result == expected
    assert result.model == "Maker/Model:free"
    assert result.api_key == "sk-MixedCaseKey"
    assert result.base_url.endswith("Box:11434/Path/v1?name=Mixed#Frag")


@pytest.mark.parametrize("scheme", ["FTP", "File", "Ws", "HTTPx"])
def test_unsupported_schemes_still_fail(scheme):
    with pytest.raises(EndpointSpecError):
        split(scheme + "://box/v1")


def test_scheme_normalization_does_not_change_wire_or_ipv6_contract():
    with pytest.raises(EndpointSpecError):
        split("OPENAI+HTTP://box/v1")
    with pytest.raises(EndpointSpecError):
        split("HTTP://[::1]/v1")
