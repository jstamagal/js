from __future__ import annotations

from unittest.mock import Mock

import pytest

from js.toolkit import process_net
from js.toolkit.core import ToolContext


@pytest.mark.parametrize("character", ["é", "界", "😀"])
def test_utf8_character_crossing_sniff_boundary_is_text(character):
    payload = ("a" * 4095 + character + "tail").encode()
    assert not process_net._looks_binary(payload)
    assert process_net._is_text_response("", payload)


@pytest.mark.parametrize("payload", [
    b"a" * 4095 + b"\xc3", b"a" * 4094 + b"\xe7\x95", b"short\xf0\x9f",
    b"a" * 100 + b"\xff" + b"a" * 5000, b"a" * 100 + b"\x00" + b"a" * 5000,
])
def test_invalid_eof_and_interior_binary_bytes_are_not_ignored(payload):
    assert process_net._looks_binary(payload)


@pytest.mark.parametrize("payload", [b"", b"plain ASCII", b"a" * 4094 + "é".encode()])
def test_complete_utf8_still_text(payload):
    assert not process_net._looks_binary(payload)


@pytest.mark.parametrize("character", ["é", "界", "😀"])
@pytest.mark.parametrize("save", [False, True])
def test_extensionless_file_fetch_preserves_text_classification(tmp_path, character, save):
    source = tmp_path / "notes"
    text = "a" * 4095 + character + "tail"
    source.write_text(text)
    result = process_net.fetch(
        source.as_uri(), save="copy" if save else None, context=ToolContext(cwd=tmp_path),
    )
    assert not result.startswith("ERROR:")
    if save:
        assert "content-type=text/plain" in result
        assert (tmp_path / "copy").read_text() == text
    else:
        assert not result.startswith("BINARY_RESPONSE")
        assert character in result


@pytest.mark.parametrize("payload, expected", [
    (("a" * 4095 + "é" + "tail").encode(), "text/plain"),
    (("a" * 4095 + "界" + "tail").encode(), "text/plain"),
    (("a" * 4095 + "😀" + "tail").encode(), "text/plain"),
    (b"a" * 4095 + b"\xc3", "application/octet-stream"),
], ids=["accent", "cjk", "emoji", "invalid-eof"])
def test_saved_download_fallback_knows_if_sniff_window_is_truncated(tmp_path, monkeypatch, payload, expected):
    target = tmp_path / "download"
    target.write_bytes(payload)
    monkeypatch.setattr(process_net.urllib.request, "urlopen", Mock(side_effect=OSError("no HEAD response")))
    assert process_net._saved_content_type("https://example.test/item", target, {}, 1) == expected
