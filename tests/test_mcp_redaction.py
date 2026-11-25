"""Credential redaction must not depend on configured-secret ordering."""
from __future__ import annotations

import asyncio

import pytest

from js.mcp import host
from js.mcp.client import MCPClient, MCPClientError
from js.mcp.types import CallToolResult


def failing_initialize(message, **options):
    """The MCPClientError an initialize raises when its transport fails with message."""
    def factory():
        raise RuntimeError(message)

    with pytest.raises(MCPClientError) as caught:
        asyncio.run(MCPClient(factory, **options).initialize())
    return str(caught.value)


@pytest.mark.parametrize("secrets", [("abc", "abcdef"), ("abcdef", "abc")])
def test_client_redacts_containing_secret_before_its_prefix(secrets):
    assert failing_initialize("key=abcdef prefix=abc", secrets=secrets) == (
        "key=[REDACTED] prefix=[REDACTED]"
    )


def test_host_without_secrets_does_not_build_per_character_candidates(monkeypatch):
    def unnecessary_normalization(value):
        pytest.fail("no-secret output needs no decoding or span allocation")

    monkeypatch.setattr(host, "_normalized_candidates", unnecessary_normalization)
    value = "%25" * 100_000
    result = host.mcp_tool_result(CallToolResult(content=[{"type": "text", "text": value}]), ())
    assert result.blocks[0]["text"] is value


def test_custom_redactor_receives_fully_scrubbed_containing_secret():
    seen = []

    def custom(value):
        seen.append(value)
        return value

    assert failing_initialize("key=abcdef", secrets=("abc", "abcdef"), redactor=custom) == "key=[REDACTED]"
    assert seen == ["key=[REDACTED]"]
