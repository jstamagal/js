from __future__ import annotations

from unittest.mock import Mock

import pytest

from js import jail
from js.toolkit import process_net
from js.toolkit.core import ToolContext


@pytest.mark.parametrize("wrapper", ["{}", "URL:{}", "<URL:{}>", "  <URL:{}>  ", "<{}>"])
@pytest.mark.parametrize("scheme", ["file", "FILE"])
def test_wrapped_file_url_uses_jail_checks(tmp_path, monkeypatch, wrapper, scheme):
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside sentinel")
    monkeypatch.setattr(jail, "ACTIVE", jail.Jail(root=root, bwrap="bwrap", private=tmp_path / "private"))
    context = ToolContext(cwd=root)
    urlopen = Mock(side_effect=AssertionError("file URL reached network backend"))
    resolver = Mock(side_effect=AssertionError("file URL reached downloader"))
    monkeypatch.setattr(process_net.urllib.request, "urlopen", urlopen)
    url = wrapper.format(outside.as_uri().replace("file:", scheme + ":", 1))

    result = process_net.fetch(url, context=context, download_resolver=resolver)

    assert result.startswith("ERROR: JailError:")
    assert "outside sentinel" not in result
    urlopen.assert_not_called()
    resolver.assert_not_called()


@pytest.mark.parametrize("wrapper", ["{}", "URL:{}", "<URL:{}>", "  <URL:{}>  "])
def test_wrapped_file_url_can_read_visible_regular_file(tmp_path, monkeypatch, wrapper):
    source = tmp_path / "visible.txt"
    source.write_text("visible sentinel")
    urlopen = Mock(side_effect=AssertionError("file URL reached network backend"))
    monkeypatch.setattr(process_net.urllib.request, "urlopen", urlopen)

    result = process_net.fetch(wrapper.format(source.as_uri()), context=ToolContext(cwd=tmp_path))

    assert "visible sentinel" in result
    urlopen.assert_not_called()


@pytest.mark.parametrize("url", [
    "data:text/plain,hello", "ftp://example.test/item", "gopher://example.test/item",
    "relative.txt", "<URL:data:text/plain,hello>", " URL:ftp://example.test/item ",
])
@pytest.mark.parametrize("save", [None, "output.bin"])
def test_unsupported_scheme_never_reaches_backends(tmp_path, monkeypatch, url, save):
    urlopen = Mock(side_effect=AssertionError("unsupported scheme reached network backend"))
    resolver = Mock(side_effect=AssertionError("unsupported scheme reached downloader"))
    monkeypatch.setattr(process_net.urllib.request, "urlopen", urlopen)

    result = process_net.fetch(url, save=save, context=ToolContext(cwd=tmp_path), download_resolver=resolver)

    assert result.startswith("ERROR:")
    urlopen.assert_not_called()
    resolver.assert_not_called()
    assert not (tmp_path / "output.bin").exists()


@pytest.mark.parametrize("url", ["http://example.test/item", "HTTPS://example.test/item"])
def test_wrapped_http_download_receives_normalized_url(tmp_path, monkeypatch, url):
    download = Mock(side_effect=lambda _binary, _url, target, **_kwargs: target.write_bytes(b"downloaded"))
    monkeypatch.setattr(process_net, "download_with_aria2", download)
    monkeypatch.setattr(process_net, "_saved_content_type", lambda *_args: "text/plain")

    result = process_net.fetch(
        "  <URL:" + url + ">  ", save="output.bin", context=ToolContext(cwd=tmp_path),
        download_resolver=lambda _name: "/fake/aria2c",
    )

    assert not result.startswith("ERROR:")
    assert download.call_args.args[1] == url
    assert (tmp_path / "output.bin").read_bytes() == b"downloaded"
