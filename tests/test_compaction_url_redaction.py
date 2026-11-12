"""Diagnostic URL copies omit credentials without changing provider configuration."""

from copy import deepcopy
import json

import pytest

from js.compaction_flight import CompactionFlight
from test_compaction_flight import _flight_cfg


@pytest.mark.parametrize("mode", ["off", "on"])
@pytest.mark.parametrize("host", ["box:8080", "[::1]:8080"])
def test_all_flight_url_copies_mask_credentials_but_preserve_routing(tmp_path, mode, host):
    url = f"https://sentinel-user:sentinel-password@{host}/v1?api_key=sentinel-query&model=useful-model&detail=high#section"
    cfg = _flight_cfg(tmp_path, tmp_path / "session.jsonl")
    cfg.paranoid_mode = mode
    cfg.provider_base_url = url
    cfg.settings["BASE_URL"] = url
    cfg.settings["EXTRA_PARAMS"] = {"endpoint": url}
    messages = [{"role": "user", "content": f"Use {url} for this request"}]
    before = deepcopy(messages)
    settings_before = deepcopy(cfg.settings)
    flight = CompactionFlight(cfg, "system", messages, trigger={"endpoint": url}, forced=True,
                              focus=None, preserve_from=0, details={"url": url}, directory=tmp_path / "flights")
    flight.write(f"GET {url}")
    flight.record("response", nested={"url": url})
    flight.finish("success", "system", messages)
    flight.close()
    raw = flight.path.read_text()
    assert "sentinel-user" not in raw
    assert "sentinel-password" not in raw
    assert "sentinel-query" not in raw
    records = [json.loads(line) for line in raw.splitlines()]
    shown = records[0]["base_url"]
    assert host in shown
    assert "/v1" in shown
    assert "model=useful-model" in shown
    assert "detail=high" in shown
    assert shown.endswith("#section")
    assert cfg.provider_base_url == url
    assert cfg.settings == settings_before
    assert messages == before
    sidecar = (tmp_path / "logs" / "session.log").read_text()
    assert "sentinel" not in sidecar


@pytest.mark.parametrize("name", ["key", "api-key", "apikey", "access_token", "refresh-token", "password", "client_secret", "authorization", "signature"])
def test_sensitive_query_names_are_redacted(tmp_path, name):
    cfg = _flight_cfg(tmp_path, tmp_path / "s.jsonl")
    cfg.provider_base_url = f"http://box/v1?{name}=sentinel-query&sort_key=name"
    flight = CompactionFlight(cfg, "", [], trigger={}, forced=True, focus=None,
                              preserve_from=0, details={}, directory=tmp_path / "flights")
    flight.close()
    raw = flight.path.read_text()
    assert "sentinel-query" not in raw
    assert "sort_key=name" in raw


def test_mixed_case_encoded_and_repeated_query_credentials(tmp_path):
    cfg = _flight_cfg(tmp_path, tmp_path / "s.jsonl")
    cfg.provider_base_url = "HTTPS://user:sentinel-password@[::1]:9443/v1?%41PI_KEY=sentinel-one&api_key=sentinel-two&model=useful"
    flight = CompactionFlight(cfg, "", [], trigger={}, forced=True, focus=None,
                              preserve_from=0, details={}, directory=tmp_path / "flights")
    flight.close()
    raw = flight.path.read_text()
    assert "sentinel" not in raw
    assert "[::1]:9443/v1" in raw
    assert "model=useful" in raw


def test_harmless_url_is_preserved_exactly():
    from js.compaction_flight import _diagnostic

    url = "HTTPS://box:8080/v1?sort_key=x&model=hello%20world&flag#section"
    assert _diagnostic(url) == url


def test_malformed_credential_url_cannot_crash_or_leak_into_flight(tmp_path):
    cfg = _flight_cfg(tmp_path, tmp_path / "s.jsonl")
    cfg.provider_base_url = "https://user:sentinel-password@[bad/v1?key=sentinel-query"
    flight = CompactionFlight(cfg, "", [], trigger={}, forced=True, focus=None,
                              preserve_from=0, details={}, directory=tmp_path / "flights")
    flight.close()
    assert "sentinel" not in flight.path.read_text()
