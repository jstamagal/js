"""Credential redaction must not depend on configured-secret ordering."""
from __future__ import annotations

import pytest

from js.mcp import host
from js.mcp.client import MCPClient


@pytest.mark.parametrize("secrets", [("abc", "abcdef"), ("abcdef", "abc")])
def test_client_redacts_containing_secret_before_its_prefix(secrets):
    client = MCPClient(lambda: None, secrets=secrets)

    assert client._redact_text("key=abcdef prefix=abc") == (
        "key=[REDACTED] prefix=[REDACTED]"
    )


def test_host_without_secrets_does_not_build_per_character_candidates(monkeypatch):
    def unnecessary_normalization(value):
        pytest.fail("no-secret output needs no decoding or span allocation")

    monkeypatch.setattr(host, "_normalized_candidates", unnecessary_normalization)
    value = "%25" * 100_000
    assert host._redact_text(value, ()) is value


def test_custom_redactor_receives_fully_scrubbed_containing_secret():
    seen = []

    def custom(value):
        seen.append(value)
        return value

    client = MCPClient(lambda: None, secrets=("abc", "abcdef"), redactor=custom)

    assert client._redact_text("key=abcdef") == "key=[REDACTED]"
    assert seen == ["key=[REDACTED]"]
