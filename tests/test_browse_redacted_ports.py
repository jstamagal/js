"""Diagnostic URLs retain the actual address without leaking credentials."""

import pytest

from js.toolkit.search import _redacted_url


@pytest.mark.parametrize("url, expected", [
    ("http://127.0.0.1:8080/a?x=1#frag", "http://127.0.0.1:8080/a"),
    ("https://user:secret@example.test:443/path?token=secret#secret", "https://example.test:443/path"),
    ("http://user:secret@[::1]:8080/a?key=secret", "http://[::1]:8080/a"),
    ("http://[2001:db8::1]/path", "http://[2001:db8::1]/path"),
    ("https://example.test/path", "https://example.test/path"),
    ("http://example.test:0/path", "http://example.test:0/path"),
])
def test_diagnostic_url_preserves_host_and_explicit_port(url, expected):
    assert _redacted_url(url) == expected


@pytest.mark.parametrize("url", [
    "http://user:secret@[invalid/path?token=secret",
    "http://user:secret@example.test:notaport/path?token=secret",
    "http://user:secret@example.test:99999/path?token=secret",
])
def test_malformed_diagnostic_url_is_safe(url):
    result = _redacted_url(url)
    assert result
    assert "secret" not in result
    assert "user" not in result
