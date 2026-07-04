from __future__ import annotations

from js.toolkit import ToolContext
from js.toolkit import fs, process_net


PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDRtiny"


class CaseInsensitiveHeaders(dict):
    def get(self, key, default=None):
        lowered = str(key).lower()
        for existing, value in self.items():
            if str(existing).lower() == lowered:
                return value
        return default


class FakeResponse:
    def __init__(self, data: bytes, headers: dict[str, str] | None = None):
        self._data = data
        self.headers = CaseInsensitiveHeaders(headers or {})

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            return self._data
        return self._data[:size]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_file_url_html_defaults_to_readable_text_and_raw_can_keep_source(tmp_path):
    page = tmp_path / "page.html"
    page.write_text("<html><body><p>Hello<br>world</p></body></html>", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    assert process_net.fetch(page.as_uri(), context=context) == "Hello\nworld"
    assert "<p>Hello" in process_net.fetch(page.as_uri(), raw=True, context=context)


def test_file_url_text_response_truncates_at_tool_result_limit(tmp_path):
    source = tmp_path / "long.txt"
    source.write_text("abcdef", encoding="utf-8")
    context = ToolContext(cwd=tmp_path, max_tool_result_bytes=4)

    result = process_net.fetch(source.as_uri(), context=context)

    assert result == "abcd\n[truncated]"


def test_file_url_binary_returns_descriptor_instead_of_bytes(tmp_path):
    blob = tmp_path / "blob.bin"
    blob.write_bytes(b"\x00\x01abc")

    result = process_net.fetch(blob.as_uri(), context=ToolContext(cwd=tmp_path))

    assert result == "BINARY_RESPONSE content-type=application/octet-stream size=5 bytes"
    assert "\x00" not in result


def test_file_url_download_writes_to_resolved_path(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"download me")
    context = ToolContext(cwd=tmp_path)
    target = tmp_path / "downloads" / "out.bin"

    result = process_net.fetch(source.as_uri(), save="downloads/out.bin", context=context)

    assert target.read_bytes() == b"download me"
    assert result == (
        f"SAVED_RESPONSE path={target} size=11 bytes content-type=application/octet-stream"
    )


def test_file_url_download_is_guarded_by_download_cap(tmp_path, monkeypatch):
    source = tmp_path / "too-big.bin"
    source.write_bytes(b"1234")
    monkeypatch.setattr(process_net, "_DOWNLOAD_MAX_BYTES", 3)

    result = process_net.fetch(source.as_uri(), save="out.bin", context=ToolContext(cwd=tmp_path))

    assert result == "ERROR: response exceeds 3 byte download limit"
    assert not (tmp_path / "out.bin").exists()


def test_file_url_image_uses_vision_marker_when_enabled_and_descriptor_when_disabled(tmp_path):
    image = tmp_path / "pixel.png"
    image.write_bytes(PNG_BYTES)

    marker = process_net.fetch(image.as_uri(), context=ToolContext(cwd=tmp_path, vision_enabled=True))
    descriptor = process_net.fetch(image.as_uri(), context=ToolContext(cwd=tmp_path))

    assert marker.startswith(fs._IMAGE_RESULT_PREFIX)
    assert marker.split("\t", 3) == [
        "IMAGE_RESULT",
        str(image),
        "image/png",
        f"VISUAL_FILE {image} mime=image/png size={len(PNG_BYTES)} bytes",
    ]
    assert descriptor == f"IMAGE_RESPONSE content-type=image/png size={len(PNG_BYTES)} bytes"


def test_http_request_builds_method_headers_and_json_body(monkeypatch, tmp_path):
    seen = {}

    def fake_urlopen(req, timeout):
        seen["req"] = req
        seen["timeout"] = timeout
        return FakeResponse(b'{"ok":true}', {"Content-Type": "application/json"})

    monkeypatch.setattr(process_net.urllib.request, "urlopen", fake_urlopen)
    context = ToolContext(cwd=tmp_path, fetch_timeout_s=7)

    result = process_net.fetch(
        "https://example.test/api",
        method="post",
        headers={"Authorization": "Bearer token", "User-Agent": "custom-agent"},
        json_body={"z": 1},
        context=context,
    )

    req = seen["req"]
    assert result == '{"ok":true}'
    assert seen["timeout"] == 7
    assert req.get_method() == "POST"
    assert req.data == b'{"z":1}'
    assert req.get_header("Authorization") == "Bearer token"
    assert req.get_header("User-agent") == "custom-agent"
    assert req.get_header("Content-type") == "application/json"


def test_http_request_accepts_header_list_and_raw_body(monkeypatch, tmp_path):
    seen = {}

    def fake_urlopen(req, timeout):
        seen["req"] = req
        return FakeResponse(b"accepted", {"Content-Type": "text/plain"})

    monkeypatch.setattr(process_net.urllib.request, "urlopen", fake_urlopen)

    result = process_net.fetch(
        "https://example.test/patch",
        method="PATCH",
        headers=["X-Test: yes"],
        body="raw body",
        context=ToolContext(cwd=tmp_path),
    )

    req = seen["req"]
    assert result == "accepted"
    assert req.get_method() == "PATCH"
    assert req.data == b"raw body"
    assert req.get_header("X-test") == "yes"
    assert req.get_header("User-agent") == "js-agent/0.1"


def test_http_binary_response_returns_descriptor(monkeypatch, tmp_path):
    def fake_urlopen(req, timeout):
        return FakeResponse(b"\x00BIN", {"Content-Type": "application/octet-stream"})

    monkeypatch.setattr(process_net.urllib.request, "urlopen", fake_urlopen)

    result = process_net.fetch("https://example.test/blob", context=ToolContext(cwd=tmp_path))

    assert result == "BINARY_RESPONSE content-type=application/octet-stream size=4 bytes"


def test_http_text_response_truncates_at_tool_result_limit(monkeypatch, tmp_path):
    def fake_urlopen(req, timeout):
        return FakeResponse(b"abcdef", {"Content-Type": "text/plain"})

    monkeypatch.setattr(process_net.urllib.request, "urlopen", fake_urlopen)
    context = ToolContext(cwd=tmp_path, max_tool_result_bytes=4)

    result = process_net.fetch("https://example.test/text", context=context)

    assert result == "abcd\n[truncated]"


def test_fetch_errors_return_error_strings_without_raising(monkeypatch, tmp_path):
    def fake_urlopen(req, timeout):
        raise RuntimeError("boom")

    monkeypatch.setattr(process_net.urllib.request, "urlopen", fake_urlopen)
    context = ToolContext(cwd=tmp_path)

    assert process_net.fetch("https://example.test/fail", context=context) == "ERROR: RuntimeError: boom"
    assert process_net.fetch("https://example.test/fail") == "ERROR: missing ToolContext"
    assert process_net.fetch(
        "https://example.test/fail", headers=["not-a-header"], context=context
    ).startswith("ERROR: ")
    assert process_net.fetch(
        "https://example.test/fail", body="raw", json_body={"x": 1}, context=context
    ).startswith("ERROR: ")


def test_fetch_tool_schema_exposes_whole_hog_surface():
    tool = next(tool for tool in process_net.tools() if tool.name == "fetch")

    assert tool.required == ("url",)
    assert set(tool.params) == {
        "url",
        "raw",
        "method",
        "headers",
        "body",
        "json_body",
        "save",
    }


def test_shell_tool_schema_exposes_timeout_param():
    """timeout has a handler default of 300s but must also be a declared schema
    param, or a schema-enforcing provider can never raise it for long builds."""
    tool = next(tool for tool in process_net.tools() if tool.name == "shell")

    assert tool.params["timeout"] == {"type": "integer", "default": 300}
