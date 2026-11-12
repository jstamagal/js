"""Endpoint ports fail during parsing rather than after a provider starts."""

import pytest

from js import cli
from js.endpoint_uri import EndpointSpecError, split


@pytest.mark.parametrize("port", ["99999", "65536", "-1", "not-a-port"])
@pytest.mark.parametrize("host", ["127.0.0.1", "[::1]"])
def test_endpoint_rejects_invalid_port(port, host):
    with pytest.raises(EndpointSpecError):
        split(f"http://Model:sk-TestKey@{host}:{port}/v1")


@pytest.mark.parametrize("port", ["", ":0", ":80", ":443", ":65535"])
@pytest.mark.parametrize("host", ["Box", "[::1]"])
def test_endpoint_accepts_port_boundaries_without_rewriting_url(port, host):
    url = f"https://{host}{port}/Path/v1?query=Value"
    result = split("responses+https://Model:sk-TestKey@" + url.removeprefix("https://"))
    assert result.model == "Model"
    assert result.api_key == "sk-TestKey"
    assert result.base_url == url
    assert result.wire == "responses"


def test_invalid_port_cli_exits_before_provider_call(capsys, monkeypatch):
    def forbid_provider(*args, **kwargs):
        pytest.fail("invalid endpoint reached provider execution")
    monkeypatch.setattr(cli, "_run_prompt", forbid_provider)
    assert cli.main(["-u", "http://Model:sk-TestKey@localhost:99999/v1", "-p", "hi"]) == 2
    captured = capsys.readouterr()
    assert captured.err
    assert "sk-TestKey" not in captured.err
