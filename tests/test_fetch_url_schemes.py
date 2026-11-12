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
